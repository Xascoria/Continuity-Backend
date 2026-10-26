import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import game, models, schemas, security
from ..auth import find_user
from ..database import get_db
from ..word_scanner import is_acceptable

router = APIRouter(prefix="/api/user", tags=["user"])

# Settled tiles are immutable within a cache_version.
TILE_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _fail(out_cls, error: game.GameError, request: Request) -> JSONResponse:
    if not error.logged:
        security.report_game_error(
            error.code, _client_ip(request), error.user_id, request.url.path)
    return JSONResponse(
        out_cls(success=False, error=error.code).model_dump(), status_code=error.status
    )


def _authenticate(db: Session, request: Request, token: str) -> models.User:
    """Resolve the token; unknown -> INVALID_TOKEN, banned -> USER_BANNED."""
    user = find_user(db, request, token)
    if user is None:
        raise game.GameError("INVALID_TOKEN")
    if game.user_is_banned(user):
        raise game.GameError("USER_BANNED", user.user_id)
    return user


@router.post("/register", response_model=schemas.RegisterOut)
def register(body: schemas.RegisterIn, db: Session = Depends(get_db)):
    """Public endpoint: create a user and issue its uuid token."""
    username = body.username.strip()
    if not is_acceptable(username):
        return schemas.RegisterOut(success=False, error="Username not allowed")

    # TODO: uniqueness rules are still to be discussed; for now it is a
    # case-insensitive exact match.
    taken = (
        db.query(models.User.user_id)
        .filter(func.lower(models.User.username) == username.lower())
        .first()
    )
    if taken:
        return schemas.RegisterOut(success=False, error="Username already taken")

    user = models.User(username=username, uuid=str(uuid.uuid4()))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # lost a race with another registration
        db.rollback()
        return schemas.RegisterOut(success=False, error="Username already taken")
    return schemas.RegisterOut(success=True, token=user.uuid)


@router.post("/request_square", response_model=schemas.SquareOut)
def request_square(
    body: schemas.SquareIn, request: Request, db: Session = Depends(get_db)
):
    try:
        user = _authenticate(db, request, body.token)
        game_type = game.validate_game_type(body.game_type)
        result = game.request_square(db, user, game_type, _client_ip(request))
    except game.GameError as e:
        return _fail(schemas.SquareOut, e, request)
    return schemas.SquareOut(success=True, **result)


@router.post("/submit_result", response_model=schemas.SubmitOut)
def submit_result(
    body: schemas.SubmitIn, request: Request, db: Session = Depends(get_db)
):
    try:
        user = _authenticate(db, request, body.token)
        result = game.submit_result(
            db, user, game_type=body.game_type, x=body.x, y=body.y,
            canvas=body.canvas, time_taken=body.time_taken,
            iteration=body.iteration, submission_id=body.submission_id,
            client_ip=_client_ip(request),
        )
    except game.GameError as e:
        return _fail(schemas.SubmitOut, e, request)
    return schemas.SubmitOut(success=True, game_type=body.game_type, **result)


@router.post("/request_leaderboard", response_model=schemas.LeaderboardOut)
def request_leaderboard(
    body: schemas.LeaderboardIn, request: Request, db: Session = Depends(get_db)
):
    try:
        user = _authenticate(db, request, body.token)
        game_type = game.validate_game_type(body.game_type)
        result = game.leaderboard(db, user, game_type)
    except game.GameError as e:
        return _fail(schemas.LeaderboardOut, e, request)
    return schemas.LeaderboardOut(success=True, **result)


@router.post("/fetch_current_progress", response_model=schemas.ProgressOut)
def fetch_current_progress(
    body: schemas.ProgressIn, request: Request, db: Session = Depends(get_db)
):
    try:
        user = _authenticate(db, request, body.token)
        game_type = game.validate_game_type(body.game_type)
        result = game.progress(db, user, game_type)
    except game.GameError as e:
        return _fail(schemas.ProgressOut, e, request)
    return schemas.ProgressOut(success=True, **result)


@router.post("/fetch_tiles", response_model=schemas.FetchTilesOut)
def fetch_tiles(
    body: schemas.FetchTilesIn, request: Request, db: Session = Depends(get_db)
):
    try:
        _authenticate(db, request, body.token)
        game_type = game.validate_game_type(body.game_type)
        result = game.fetch_tiles(db, game_type, [t.model_dump() for t in body.tiles])
    except game.GameError as e:
        return _fail(schemas.FetchTilesOut, e, request)
    return JSONResponse(
        schemas.FetchTilesOut(success=True, **result).model_dump(),
        headers=TILE_CACHE_HEADERS,
    )
