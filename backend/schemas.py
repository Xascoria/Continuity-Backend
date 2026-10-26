from datetime import datetime
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class HealthOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    created_at: datetime


# ---- user endpoints -------------------------------------------------------
# Structurally malformed input (wrong JSON types) is rejected by FastAPI with
# 422. Game-level problems (bad token, game_type, canvas, ...) are reported
# with success=false and a machine-readable error code (see game.ERROR_STATUS)
# and a matching HTTP status. game_type is validated in-band (INVALID_GAME_TYPE).


class RegisterIn(BaseModel):
    username: str


class RegisterOut(BaseModel):
    success: bool
    token: Optional[str] = None
    error: Optional[str] = None


class SquareIn(BaseModel):
    token: str
    game_type: int


class CanvasLevel(BaseModel):
    kind: str = "canvas"
    width: int
    height: int
    pixels: str
    source_iteration: int


class Rect(BaseModel):
    x: int
    y: int
    width: int
    height: int


class ContextRegion(Rect):
    pixels: str
    source_iteration: int


class ContextLevel(BaseModel):
    kind: str = "context"
    target: Rect
    window: Rect
    regions: List[ContextRegion]


class SquareOut(BaseModel):
    success: bool
    x: Optional[int] = None
    y: Optional[int] = None
    game_type: Optional[int] = None
    iteration: Optional[int] = None
    level_information: Optional[Union[CanvasLevel, ContextLevel]] = None
    error: Optional[str] = None


class SubmitIn(BaseModel):
    token: str
    x: int
    y: int
    canvas: str
    time_taken: float = Field(ge=0)
    game_type: int
    iteration: int = Field(ge=0)
    submission_id: str


class SubmitOut(BaseModel):
    success: bool
    x: Optional[int] = None
    y: Optional[int] = None
    game_type: Optional[int] = None
    iteration: Optional[int] = None
    accuracy: Optional[float] = None  # ratio 0-1
    normalized_canvas: Optional[str] = None
    error: Optional[str] = None


class LeaderboardIn(BaseModel):
    token: str
    game_type: int


class LeaderboardEntry(BaseModel):
    rank: int
    username: str
    count: int
    accuracy: float  # mean per-submission accuracy, ratio 0-1
    is_current_user: bool = False


class LeaderboardOut(BaseModel):
    success: bool
    top_contributors: List[LeaderboardEntry] = []
    top_accuracy: List[LeaderboardEntry] = []
    error: Optional[str] = None


class ProgressIn(BaseModel):
    token: str
    game_type: int


class Grid(BaseModel):
    columns: int
    rows: int
    tile_size: int


class OpenIteration(BaseModel):
    iteration: int
    filled_tiles: int
    total_tiles: int


class PlayerStats(BaseModel):
    contribution_count: int
    mean_accuracy: float


class ProgressOut(BaseModel):
    success: bool
    game_type: Optional[int] = None
    state_version: Optional[int] = None
    cache_version: Optional[str] = None
    grid: Optional[Grid] = None
    open_iterations: List[OpenIteration] = []
    latest_iteration: List[int] = []
    player: Optional[PlayerStats] = None
    error: Optional[str] = None


class TileRef(BaseModel):
    iteration: int
    x: int
    y: int


class TileOut(TileRef):
    pixels: str


class FetchTilesIn(BaseModel):
    token: str
    game_type: int
    tiles: List[TileRef]


class FetchTilesOut(BaseModel):
    success: bool
    game_type: Optional[int] = None
    cache_version: Optional[str] = None
    tiles: List[TileOut] = []
    missing: List[TileRef] = []
    error: Optional[str] = None
