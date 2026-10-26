from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import Boolean, DateTime, Float, Integer, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import models, schemas
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
