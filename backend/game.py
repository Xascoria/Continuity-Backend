"""Game logic (see backend_design.md).

All game state is derived from the database (submissions, tile_holds,
game_state), so it survives restarts and works with several workers. A
process-wide lock serialises allocation/submission; the unique indexes are the
safety net across processes.
"""
import json
import math
import random
import threading
import uuid
from datetime import timedelta
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, security
from .config import ORIGINAL_IMAGE_FILE
from .models import utcnow

# ---- constants (section 2) --------------------------------------------------

GRID_COLUMNS = 15
GRID_ROWS = 12
TILE_SIZE = 32
TILE_PIXELS = TILE_SIZE * TILE_SIZE  # 1024
TOTAL_TILES = GRID_COLUMNS * GRID_ROWS  # 180
IMAGE_WIDTH = GRID_COLUMNS * TILE_SIZE  # 480
IMAGE_HEIGHT = GRID_ROWS * TILE_SIZE  # 384
WINDOW_SIZE = 64

PALETTE = "123456789abc"  # 12 colour symbols
# Hex colours for symbols 1-9, a-c (client rendering and the admin visualizer only).
PALETTE_HEX = ["#cdd4d0", "#87a2bf", "#2f4f93", "#0e2948", "#85934e", "#5a732b",
               "#27412d", "#0e1612", "#c4b257", "#ba9511", "#6c392c", "#2b1f12"]
UNPAINTED = "."
FILL = "1"
ALPHABET = frozenset(PALETTE + UNPAINTED)

HOLD_SECONDS = 40
HOLD_LIMIT = 5
MIN_PAINTED = math.ceil(0.60 * TILE_PIXELS)  # 615
MAX_FETCH_TILES = 100
LEADERBOARD_MIN_CONTRIBUTIONS = 10
LEADERBOARD_LIST_SIZE = 10

# Bump when the image, palette symbol table or rules change (clients drop caches).
CACHE_VERSION = "1"

# ---- errors (section 12) ------------------------------------------------------

ERROR_STATUS = {
    "INVALID_TOKEN": 401,
    "USER_BANNED": 403,
    "INVALID_GAME_TYPE": 400,
    "INVALID_COORDINATE": 400,
    "CANVAS_INVALID": 400,
    "INSUFFICIENT_COVERAGE": 400,
    "INVALID_SUBMISSION_ID": 400,
    "TOO_MANY_TILES": 400,
    "HOLD_NOT_FOUND": 404,
    "HOLD_EXPIRED": 410,
    "SUBMISSION_ID_REUSED": 409,
    "TILE_ALREADY_SUBMITTED": 409,
    "NO_SUBMISSION": 404,  # /api/user/health when game type 1 has no submissions
    "INTERNAL_ERROR": 500,
}


class GameError(Exception):
    def __init__(self, code: str, user_id=None, logged: bool = False):
        super().__init__(code)
        self.code = code
        self.status = ERROR_STATUS.get(code, 400)
        self.user_id = user_id
        self.logged = logged  # already recorded as a suspicious event


_LOCK = threading.RLock()

# ---- original image (iteration 0) ---------------------------------------------

_original_rows: Optional[List[str]] = None


def _load_original() -> Optional[List[str]]:
    """image/original.txt: 384 lines of 480 palette symbols. Missing -> blank."""
    try:
        text = ORIGINAL_IMAGE_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    rows = text.split()
    if len(rows) != IMAGE_HEIGHT or any(
        len(r) != IMAGE_WIDTH or not set(r) <= set(PALETTE) for r in rows
    ):
        raise RuntimeError(
            f"{ORIGINAL_IMAGE_FILE} must be {IMAGE_HEIGHT} lines of "
            f"{IMAGE_WIDTH} symbols from {PALETTE!r}"
        )
    return rows


def original_tile(x: int, y: int) -> str:
    global _original_rows
    if _original_rows is None:
        _original_rows = _load_original() or []
    if not _original_rows:
        return FILL * TILE_PIXELS
    x0, y0 = x * TILE_SIZE, y * TILE_SIZE
    return "".join(
        _original_rows[y0 + r][x0 : x0 + TILE_SIZE] for r in range(TILE_SIZE)
    )


# ---- validation helpers -------------------------------------------------------


def validate_game_type(game_type) -> int:
    if isinstance(game_type, bool) or game_type not in (1, 2):
        raise GameError("INVALID_GAME_TYPE")
    return game_type


def validate_coordinate(x, y) -> None:
    if not (0 <= x < GRID_COLUMNS and 0 <= y < GRID_ROWS):
        raise GameError("INVALID_COORDINATE")


def validate_submission_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        raise GameError("INVALID_SUBMISSION_ID")
    return str(parsed)


def validate_canvas(canvas: str) -> None:
    if len(canvas) != TILE_PIXELS or not set(canvas) <= ALPHABET:
        raise GameError("CANVAS_INVALID")


def user_is_banned(user: models.User) -> bool:
    return user.banned_at is not None


# ---- tile state ------------------------------------------------------------------


def _accepted_counts(db: Session, model) -> Dict[int, int]:
    rows = (
        db.query(model.iteration, func.count())
        .filter(model.x.isnot(None), model.y.isnot(None))
        .group_by(model.iteration)
        .all()
    )
    return {it: n for it, n in rows}


def _highest_known_iteration(db: Session, model, game_type: int) -> int:
    a = (
        db.query(func.max(model.iteration))
        .filter(model.x.isnot(None), model.y.isnot(None))
        .scalar()
    )
    h = (
        db.query(func.max(models.TileHold.iteration))
        .filter(models.TileHold.game_type == game_type)
        .scalar()
    )
    return max(a or 0, h or 0)


def _free_tiles(db: Session, model, game_type: int, iteration: int, now) -> List[Tuple[int, int]]:
    taken: Set[Tuple[int, int]] = set(
        db.query(model.x, model.y)
        .filter(model.iteration == iteration, model.x.isnot(None))
        .all()
    )
    taken |= set(
        db.query(models.TileHold.x, models.TileHold.y).filter(
            models.TileHold.game_type == game_type,
            models.TileHold.iteration == iteration,
            models.TileHold.expires_at > now,
        )
    )
    return [
        (x, y)
        for y in range(GRID_ROWS)
        for x in range(GRID_COLUMNS)
        if (x, y) not in taken
    ]


def resolve_reference(db: Session, model, iteration: int, x: int, y: int) -> int:
    """Nearest lower iteration in which the tile is settled (0 = original)."""
    found = (
        db.query(func.max(model.iteration))
        .filter(model.x == x, model.y == y, model.iteration < iteration)
        .scalar()
    )
    return found or 0


def tile_pixels(db: Session, model, iteration: int, x: int, y: int) -> Optional[str]:
    """Pixels of a settled tile, or None if it is not settled."""
    if iteration == 0:
        return original_tile(x, y)
    return (
        db.query(model.submitted_string)
        .filter(model.iteration == iteration, model.x == x, model.y == y)
        .scalar()
    )


# ---- level information (sections 4.2, 4.3, 5) ---------------------------------


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _crop(tile: str, x0: int, y0: int, w: int, h: int) -> str:
    return "".join(
        tile[(y0 + r) * TILE_SIZE + x0 : (y0 + r) * TILE_SIZE + x0 + w]
        for r in range(h)
    )


def context_layout(x: int, y: int) -> Tuple[Tuple[int, int], List[Tuple[int, int, int, int]]]:
    """Window origin and the context cells (x, y, w, h in image pixels).

    Cells are cut along tile boundaries so each lies inside one source tile.
    """
    wx = _clamp(TILE_SIZE * x - 16, 0, IMAGE_WIDTH - WINDOW_SIZE)
    wy = _clamp(TILE_SIZE * y - 16, 0, IMAGE_HEIGHT - WINDOW_SIZE)

    def breaks(start: int, limit: int) -> List[int]:
        inner = [m for m in range(0, limit + 1, TILE_SIZE) if start < m < start + WINDOW_SIZE]
        return sorted({start, start + WINDOW_SIZE, *inner})

    xs, ys = breaks(wx, IMAGE_WIDTH), breaks(wy, IMAGE_HEIGHT)
    tx, ty = x * TILE_SIZE, y * TILE_SIZE
    cells = []
    for y0, y1 in zip(ys, ys[1:]):
        for x0, x1 in zip(xs, xs[1:]):
            if x0 >= tx and x1 <= tx + TILE_SIZE and y0 >= ty and y1 <= ty + TILE_SIZE:
                continue  # the target tile itself
            cells.append((x0, y0, x1 - x0, y1 - y0))
    return (wx, wy), cells


def build_level(db: Session, model, game_type: int, iteration: int, x: int, y: int):
    """Returns (level_information dict, reference_iteration, context_sources)."""
    reference = resolve_reference(db, model, iteration, x, y)
    if game_type == 1:
        pixels = tile_pixels(db, model, reference, x, y)
        level = {
            "kind": "canvas",
            "width": TILE_SIZE,
            "height": TILE_SIZE,
            "pixels": pixels,
            "source_iteration": reference,
        }
        return level, reference, None

    (wx, wy), cells = context_layout(x, y)
    tiles: Dict[Tuple[int, int], Tuple[int, str]] = {}
    regions, sources = [], []
    for cx, cy, w, h in cells:
        key = (cx // TILE_SIZE, cy // TILE_SIZE)
        if key not in tiles:
            src = resolve_reference(db, model, iteration, *key)
            tiles[key] = (src, tile_pixels(db, model, src, *key))
        src, tile = tiles[key]
        regions.append({
            "x": cx, "y": cy, "width": w, "height": h,
            "pixels": _crop(tile, cx % TILE_SIZE, cy % TILE_SIZE, w, h),
            "source_iteration": src,
        })
        sources.append(src)
    level = {
        "kind": "context",
        "target": {"x": x * TILE_SIZE, "y": y * TILE_SIZE, "width": TILE_SIZE, "height": TILE_SIZE},
        "window": {"x": wx, "y": wy, "width": WINDOW_SIZE, "height": WINDOW_SIZE},
        "regions": regions,
    }
    return level, reference, sources


# ---- request_square ------------------------------------------------------------


def request_square(db: Session, user: models.User, game_type: int,
                   client_ip: Optional[str] = None) -> dict:
    model = models.SUBMISSION_MODELS[game_type]
    with _LOCK:
        for _attempt in range(3):
            now = utcnow()
            # A new request abandons the user's previous hold(s) for this game
            # type, so they free up immediately.
            db.query(models.TileHold).filter(
                models.TileHold.user_id == user.user_id,
                models.TileHold.game_type == game_type,
            ).delete()
            active = (
                db.query(func.count(models.TileHold.id))
                .filter(models.TileHold.user_id == user.user_id, models.TileHold.expires_at > now)
                .scalar()
            )
            if active >= HOLD_LIMIT:
                user.banned_at = now
                db.query(models.TileHold).filter(
                    models.TileHold.user_id == user.user_id
                ).delete()
                db.commit()
                security.log_event(
                    "AUTO_BAN", security.CRITICAL, client_ip, user.user_id,
                    {"active_holds": active, "limit": HOLD_LIMIT, "game_type": game_type})
                raise GameError("USER_BANNED", user.user_id, logged=True)

            counts = _accepted_counts(db, model)
            top = _highest_known_iteration(db, model, game_type)
            chosen = None
            for iteration in range(1, top + 1):
                if counts.get(iteration, 0) >= TOTAL_TILES:
                    continue
                free = _free_tiles(db, model, game_type, iteration, now)
                if free:
                    chosen = (iteration, random.choice(free))
                    break
            if chosen is None:  # open the next iteration
                chosen = (top + 1, (random.randrange(GRID_COLUMNS), random.randrange(GRID_ROWS)))
            iteration, (x, y) = chosen

            level, reference, sources = build_level(db, model, game_type, iteration, x, y)
            try:
                # An expired hold on this tile is overwritten.
                db.query(models.TileHold).filter(
                    models.TileHold.game_type == game_type,
                    models.TileHold.iteration == iteration,
                    models.TileHold.x == x,
                    models.TileHold.y == y,
                ).delete()
                db.add(models.TileHold(
                    game_type=game_type, iteration=iteration, x=x, y=y,
                    user_id=user.user_id, acquired_at=now,
                    expires_at=now + timedelta(seconds=HOLD_SECONDS),
                    reference_iteration=reference,
                    context_sources=json.dumps(sources) if sources is not None else None,
                ))
                db.commit()
            except IntegrityError:  # another worker took it; try again
                db.rollback()
                continue
            return {"x": x, "y": y, "game_type": game_type,
                    "iteration": iteration, "level_information": level}
    raise GameError("INTERNAL_ERROR")


# ---- submit_result --------------------------------------------------------------


def _replay_response(sub) -> dict:
    return {"x": sub.x, "y": sub.y, "iteration": sub.iteration,
            "accuracy": sub.accuracy, "normalized_canvas": sub.submitted_string}


def _bump_state_version(db: Session, game_type: int) -> None:
    state = db.get(models.GameState, game_type)
    if state is None:
        db.add(models.GameState(game_type=game_type, state_version=1))
    else:
        state.state_version += 1


def _submit_result(db: Session, user: models.User, *, game_type, x, y, canvas,
                  time_taken, iteration, submission_id,
                  client_ip: Optional[str] = None) -> dict:
    submission_id = validate_submission_id(submission_id)
    validate_game_type(game_type)
    model = models.SUBMISSION_MODELS[game_type]
    other = models.SUBMISSION_MODELS[3 - game_type]

    with _LOCK:
        # 2. idempotency
        existing = (
            db.query(model)
            .filter(model.user_id == user.user_id, model.submission_id == submission_id)
            .first()
        )
        if existing is not None:
            if (existing.x, existing.y, existing.iteration) != (x, y, iteration):
                raise GameError("SUBMISSION_ID_REUSED")
            return _replay_response(existing)
        if db.query(other.id).filter(
            other.user_id == user.user_id, other.submission_id == submission_id
        ).first():
            raise GameError("SUBMISSION_ID_REUSED")

        # 3. validation
        validate_coordinate(x, y)
        validate_canvas(canvas)

        # 4. tile already accepted
        if db.query(model.id).filter(
            model.iteration == iteration, model.x == x, model.y == y
        ).first():
            raise GameError("TILE_ALREADY_SUBMITTED")

        # 5. hold
        hold = (
            db.query(models.TileHold)
            .filter(
                models.TileHold.user_id == user.user_id,
                models.TileHold.game_type == game_type,
                models.TileHold.iteration == iteration,
                models.TileHold.x == x,
                models.TileHold.y == y,
            )
            .first()
        )
        if hold is None:
            raise GameError("HOLD_NOT_FOUND")
        now = utcnow()
        if hold.expires_at <= now:
            raise GameError("HOLD_EXPIRED")

        # 6-7. coverage and normalisation
        if len(canvas) - canvas.count(UNPAINTED) < MIN_PAINTED:
            raise GameError("INSUFFICIENT_COVERAGE")
        normalized = canvas.replace(UNPAINTED, FILL)

        # 8. accuracy against the reference frozen on the hold
        reference = tile_pixels(db, model, hold.reference_iteration, x, y)
        matching = sum(a == b for a, b in zip(normalized, reference))
        accuracy = matching / TILE_PIXELS

        # Most recent earlier submission of this user (either game type), for
        # the fast-submission check below.
        previous = max(
            (t for t in (
                db.query(func.max(m.created_at)).filter(m.user_id == user.user_id).scalar()
                for m in models.SUBMISSION_MODELS.values()) if t is not None),
            default=None,
        )

        # 9. persist
        submission = model(
            user_id=user.user_id, submitted_string=normalized, accuracy=accuracy,
            iteration=iteration, x=x, y=y, submission_id=submission_id,
            reference_iteration=hold.reference_iteration,
            context_sources=hold.context_sources, time_taken=time_taken,
        )
        db.add(submission)
        db.delete(hold)
        _bump_state_version(db, game_type)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise GameError("TILE_ALREADY_SUBMITTED")

        # Suspicious-activity checks (logged only; the submission is accepted).
        if previous is not None:
            gap = (submission.created_at - previous).total_seconds()
            if gap < security.FAST_SUBMIT_SECONDS:
                security.log_event(
                    "FAST_SUBMISSIONS", security.WARNING, client_ip, user.user_id,
                    {"seconds_since_previous": round(gap, 2), "game_type": game_type,
                     "iteration": iteration, "x": x, "y": y},
                    cooldown_key=(user.user_id,), cooldown=10)
        if time_taken < security.MIN_PLAUSIBLE_TIME_TAKEN:
            security.log_event(
                "IMPOSSIBLE_TIME_TAKEN", security.WARNING, client_ip, user.user_id,
                {"time_taken": time_taken, "game_type": game_type},
                cooldown_key=(user.user_id,), cooldown=10)
        return _replay_response(submission)


def submit_result(db: Session, user: models.User, **kwargs) -> dict:
    try:
        return _submit_result(db, user, **kwargs)
    except GameError as e:
        if e.user_id is None:
            e.user_id = user.user_id
        raise


# ---- request_leaderboard ----------------------------------------------------------


def leaderboard(db: Session, user: models.User, game_type: int) -> dict:
    model = models.SUBMISSION_MODELS[game_type]
    count = func.count(model.id)
    rows = (
        db.query(models.User.user_id, models.User.username, count, func.avg(model.accuracy))
        .join(model, model.user_id == models.User.user_id)
        .group_by(models.User.user_id)
        .having(count >= LEADERBOARD_MIN_CONTRIBUTIONS)
        .all()
    )
    players = [
        {"user_id": uid, "username": name, "count": n, "accuracy": acc or 0.0}
        for uid, name, n, acc in rows
    ]

    def ranked(metric: str) -> List[dict]:
        ordered = sorted(
            players, key=lambda p: (-p[metric], p["username"].lower(), p["user_id"])
        )
        entries = [
            {"rank": i + 1, "username": p["username"], "count": p["count"],
             "accuracy": p["accuracy"], "is_current_user": p["user_id"] == user.user_id,
             "_uid": p["user_id"]}
            for i, p in enumerate(ordered)
        ]
        top = entries[:LEADERBOARD_LIST_SIZE]
        if not any(e["is_current_user"] for e in top):
            top += [e for e in entries if e["is_current_user"]]
        for e in top:
            del e["_uid"]
        return top

    return {"top_contributors": ranked("count"), "top_accuracy": ranked("accuracy")}


# ---- fetch_current_progress / fetch_tiles -------------------------------------------


def progress(db: Session, user: models.User, game_type: int) -> dict:
    model = models.SUBMISSION_MODELS[game_type]
    latest = [0] * TOTAL_TILES
    counts: Dict[int, int] = {}
    for it, x, y in db.query(model.iteration, model.x, model.y).filter(
        model.x.isnot(None), model.y.isnot(None)
    ):
        counts[it] = counts.get(it, 0) + 1
        i = y * GRID_COLUMNS + x
        if it > latest[i]:
            latest[i] = it
    top = _highest_known_iteration(db, model, game_type)
    open_iterations = [
        {"iteration": it, "filled_tiles": counts.get(it, 0), "total_tiles": TOTAL_TILES}
        for it in range(1, top + 1)
        if counts.get(it, 0) < TOTAL_TILES
    ]
    n, mean = (
        db.query(func.count(model.id), func.avg(model.accuracy))
        .filter(model.user_id == user.user_id)
        .one()
    )
    state = db.get(models.GameState, game_type)
    return {
        "game_type": game_type,
        "state_version": state.state_version if state else 0,
        "cache_version": CACHE_VERSION,
        "grid": {"columns": GRID_COLUMNS, "rows": GRID_ROWS, "tile_size": TILE_SIZE},
        "open_iterations": open_iterations,
        "latest_iteration": latest,
        "player": {"contribution_count": n or 0, "mean_accuracy": mean or 0.0},
    }


def fetch_tiles(db: Session, game_type: int, tiles: List[dict]) -> dict:
    if len(tiles) > MAX_FETCH_TILES:
        raise GameError("TOO_MANY_TILES")
    model = models.SUBMISSION_MODELS[game_type]
    found, missing = [], []
    for t in tiles:
        validate_coordinate(t["x"], t["y"])
        # Iteration 0 is bundled with the client, so it is never served.
        pixels = None if t["iteration"] <= 0 else tile_pixels(
            db, model, t["iteration"], t["x"], t["y"])
        if pixels is None:
            missing.append(t)
        else:
            found.append({**t, "pixels": pixels})
    return {"game_type": game_type, "cache_version": CACHE_VERSION,
            "tiles": found, "missing": missing}
