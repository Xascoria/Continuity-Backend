import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, Integer, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import game, models, schemas, security
from ..auth import require_admin
from ..database import Base, get_db

router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)

MAX_PAGE_SIZE = 200


@router.post("/login")
def login():
    """Credentials are checked by the router dependency; reaching here = valid."""
    return {"success": True}


# ---- health ----------------------------------------------------------------


@router.get("/health", response_model=schemas.HealthOut)
def check_health(db: Session = Depends(get_db)):
    """Verify the DB is reachable and record the check in the `health` table."""
    db.execute(select(1))
    record = models.Health(status="ok")
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/health/history", response_model=List[schemas.HealthOut])
def health_history(limit: int = 10, db: Session = Depends(get_db)):
    return (
        db.query(models.Health)
        .order_by(models.Health.id.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )


# ---- suspicious events -----------------------------------------------------


@router.get("/suspicious_events")
def suspicious_events(
    limit: int = 50,
    offset: int = 0,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    client_ip: Optional[str] = None,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Newest first, with optional filters, plus per-type counts for the last 24h."""
    E = models.SuspiciousEvent
    query = db.query(E)
    for column, value in ((E.event_type, event_type), (E.severity, severity),
                          (E.client_ip, client_ip), (E.user_id, user_id)):
        if value not in (None, ""):
            query = query.filter(column == value)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    since = models.utcnow() - timedelta(hours=24)
    summary = dict(
        db.query(E.event_type, func.count()).filter(E.created_at >= since)
        .group_by(E.event_type).all()
    )
    rows = query.order_by(E.id.desc()).limit(limit).offset(max(offset, 0)).all()
    banned_users = {
        uid for (uid,) in db.query(models.User.user_id).filter(
            models.User.user_id.in_({r.user_id for r in rows if r.user_id is not None}),
            models.User.banned_at.isnot(None))
    }
    known_users = {
        uid for (uid,) in db.query(models.User.user_id).filter(
            models.User.user_id.in_({r.user_id for r in rows if r.user_id is not None}))
    }
    banned_ips = {
        ip for (ip,) in db.query(models.BannedIp.ip).filter(
            models.BannedIp.ip.in_({r.client_ip for r in rows if r.client_ip}))
    }
    return {
        "total": query.count(),
        "limit": limit,
        "offset": max(offset, 0),
        "last_24h": summary,
        "events": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "event_type": r.event_type,
                "severity": r.severity,
                "client_ip": r.client_ip,
                "user_id": r.user_id,
                "details": json.loads(r.details or "{}"),
                # None = not applicable (no user / no ip, or the user was deleted)
                "user_banned": (r.user_id in banned_users) if r.user_id in known_users else None,
                "ip_banned": (r.client_ip in banned_ips) if r.client_ip else None,
            }
            for r in rows
        ],
    }


# ---- visualizer ---------------------------------------------------------


@router.get("/visualizer")
def visualizer_summary(game_type: int, db: Session = Depends(get_db)):
    """Iterations of a game type with their filled tile counts (iteration 0 = original)."""
    model = models.SUBMISSION_MODELS.get(game_type)
    if model is None:
        raise HTTPException(status_code=400, detail="game_type must be 1 or 2")
    counts = dict(
        db.query(model.iteration, func.count())
        .filter(model.x.isnot(None), model.y.isnot(None))
        .group_by(model.iteration).all()
    )
    iterations = [{"iteration": 0, "filled_tiles": game.TOTAL_TILES}] + [
        {"iteration": it, "filled_tiles": n} for it, n in sorted(counts.items()) if it > 0
    ]
    return {
        "game_type": game_type,
        "palette": dict(zip(game.PALETTE, game.PALETTE_HEX)),
        "grid": {"columns": game.GRID_COLUMNS, "rows": game.GRID_ROWS,
                 "tile_size": game.TILE_SIZE},
        "total_tiles": game.TOTAL_TILES,
        "iterations": iterations,
    }


@router.get("/visualizer/iteration")
def visualizer_iteration(game_type: int, iteration: int, db: Session = Depends(get_db)):
    """All settled tiles of one iteration (iteration 0 = the original image)."""
    model = models.SUBMISSION_MODELS.get(game_type)
    if model is None or iteration < 0:
        raise HTTPException(status_code=400, detail="invalid game_type or iteration")
    if iteration == 0:
        tiles = [
            {"x": x, "y": y, "pixels": game.original_tile(x, y), "username": None,
             "accuracy": None}
            for y in range(game.GRID_ROWS) for x in range(game.GRID_COLUMNS)
        ]
    else:
        rows = (
            db.query(model.x, model.y, model.submitted_string, model.accuracy,
                     models.User.username)
            .join(models.User, models.User.user_id == model.user_id)
            .filter(model.iteration == iteration, model.x.isnot(None))
            .all()
        )
        tiles = [{"x": x, "y": y, "pixels": p, "accuracy": a, "username": u}
                 for x, y, p, a, u in rows]
    return {"game_type": game_type, "iteration": iteration, "tiles": tiles}


# ---- bans ------------------------------------------------------------------


def _get_user(db: Session, user_id: int) -> models.User:
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/users/{user_id}/ban")
def ban_user(user_id: int, db: Session = Depends(get_db)):
    """Ban a user token: it gets USER_BANNED on every user endpoint."""
    user = _get_user(db, user_id)
    if user.banned_at is None:
        user.banned_at = models.utcnow()
    db.query(models.TileHold).filter(models.TileHold.user_id == user_id).delete()
    db.commit()
    return {"success": True, "user_id": user_id, "banned": True}


@router.post("/users/{user_id}/unban")
def unban_user(user_id: int, db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    user.banned_at = None
    db.commit()
    return {"success": True, "user_id": user_id, "banned": False}


class IpIn(BaseModel):
    ip: str = Field(min_length=1, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=200)


@router.post("/ban_ip")
def ban_ip(body: IpIn, db: Session = Depends(get_db)):
    """Refuse every non-admin /api request from this IP (HTTP 403 IP_BANNED)."""
    ip = body.ip.strip()
    if not db.query(models.BannedIp.id).filter(models.BannedIp.ip == ip).first():
        db.add(models.BannedIp(ip=ip, reason=body.reason))
        db.commit()
    security.ip_bans.invalidate()
    return {"success": True, "ip": ip, "banned": True}


@router.post("/unban_ip")
def unban_ip(body: IpIn, db: Session = Depends(get_db)):
    db.query(models.BannedIp).filter(models.BannedIp.ip == body.ip.strip()).delete()
    db.commit()
    security.ip_bans.invalidate()
    return {"success": True, "ip": body.ip.strip(), "banned": False}


# ---- generic table editor --------------------------------------------------


def _get_table(name: str):
    table = Base.metadata.tables.get(name)
    if table is None:
        raise HTTPException(status_code=404, detail=f"Unknown table: {name}")
    return table


def _pk_column(table):
    pks = list(table.primary_key.columns)
    if len(pks) != 1:
        raise HTTPException(status_code=400, detail="Table needs a single primary key")
    return pks[0]


def _serialize(row) -> Dict[str, Any]:
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v)
        for k, v in row._mapping.items()
    }


def _coerce(column, value):
    """Convert a JSON value to the Python type the column expects."""
    if value is None:
        return None
    t = column.type
    is_text = not isinstance(t, (DateTime, Integer, Float, Boolean))
    if isinstance(value, str) and value == "" and not is_text:
        return None
    try:
        if isinstance(t, DateTime):
            return datetime.fromisoformat(str(value).replace("Z", ""))
        if isinstance(t, Boolean):
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true")
        if isinstance(t, Integer):
            return int(value)
        if isinstance(t, Float):
            return float(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail=f"Invalid value for column '{column.name}': {value!r}"
        )
    return str(value)


def _values(table, payload: Dict[str, Any], *, creating: bool) -> Dict[str, Any]:
    pk = _pk_column(table)
    values = {}
    for name, raw in payload.items():
        column = table.columns.get(name)
        if column is None:
            raise HTTPException(status_code=400, detail=f"Unknown column: {name}")
        if name == pk.name and not creating:
            continue  # primary keys are not editable
        if creating and (raw is None or raw == ""):
            continue  # leave it to the column default / autoincrement
        values[name] = _coerce(column, raw)
    return values


def _run(db: Session, statement):
    try:
        result = db.execute(statement)
        db.commit()
        return result
    except SQLAlchemyError as e:
        db.rollback()
        detail = str(getattr(e, "orig", None) or e)
        raise HTTPException(status_code=400, detail=detail)


def _get_row(db: Session, table, pk_value: int):
    row = db.execute(select(table).where(_pk_column(table) == pk_value)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found")
    return row


@router.get("/tables")
def list_tables():
    return [
        {
            "name": name,
            "primary_key": _pk_column(table).name,
            "columns": [
                {
                    "name": c.name,
                    "type": str(c.type),
                    "primary_key": c.primary_key,
                    "nullable": c.nullable,
                    "has_default": c.primary_key
                    or c.default is not None
                    or c.server_default is not None,
                }
                for c in table.columns
            ],
        }
        for name, table in sorted(Base.metadata.tables.items())
    ]


@router.get("/tables/{name}/rows")
def list_rows(name: str, limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    table = _get_table(name)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    total = db.execute(select(func.count()).select_from(table)).scalar_one()
    rows = db.execute(
        select(table)
        .order_by(_pk_column(table).desc())  # newest first
        .limit(limit)
        .offset(max(offset, 0))
    ).all()
    return {
        "total": total,
        "limit": limit,
        "offset": max(offset, 0),
        "rows": [_serialize(r) for r in rows],
    }


@router.post("/tables/{name}/rows")
def create_row(
    name: str, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    table = _get_table(name)
    result = _run(db, table.insert().values(**_values(table, payload, creating=True)))
    return _serialize(_get_row(db, table, result.inserted_primary_key[0]))


@router.put("/tables/{name}/rows/{pk}")
def update_row(
    name: str,
    pk: int,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    table = _get_table(name)
    _get_row(db, table, pk)
    values = _values(table, payload, creating=False)
    if values:
        _run(db, table.update().where(_pk_column(table) == pk).values(**values))
    return _serialize(_get_row(db, table, pk))


@router.delete("/tables/{name}/rows/{pk}")
def delete_row(name: str, pk: int, db: Session = Depends(get_db)):
    table = _get_table(name)
    _get_row(db, table, pk)
    _run(db, table.delete().where(_pk_column(table) == pk))
    return {"success": True}
