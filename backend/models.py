from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import declared_attr, mapped_column

from .database import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite does not store timezones)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    # Every table has created_at (sort_order keeps it the last column).
    created_at = mapped_column(DateTime, nullable=False, default=utcnow, sort_order=100)


class Health(TimestampMixin, Base):
    __tablename__ = "health"

    id = Column(Integer, primary_key=True)
    status = Column(String, nullable=False, default="ok")


class _RequestLogBase(TimestampMixin, Base):
    """Columns shared by both request logs (see request_log.py)."""

    __abstract__ = True

    id = Column(Integer, primary_key=True)
    method = Column(String(10), nullable=False)
    path = Column(String, nullable=False)
    query_string = Column(Text)
    client_ip = Column(String)
    headers = Column(Text)  # JSON object; credentials (Authorization/Cookie) never stored
    request_body = Column(Text)
    response_status = Column(Integer)
    response_body = Column(Text)
    duration_ms = Column(Float)


class UsersRequest(_RequestLogBase):
    """Every /api request that is not an admin request. Tokens are kept as-is."""

    __tablename__ = "users_requests"

    # sort_order places these after the shared columns and before created_at.

    # The user token (uuid) the request carried; for register, the token it
    # issued. Kept even when it matches no user.
    token = mapped_column(String, index=True, sort_order=10)
    # Set when the token resolved to a user. Deliberately not a foreign key so
    # deleting a user never blocks on / erases the log.
    user_id = mapped_column(Integer, index=True, sort_order=11)


class AdminRequest(_RequestLogBase):
    """Every /api/admin request. User tokens (uuids) are redacted."""

    __tablename__ = "admin_requests"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(25), nullable=False)
    uuid = Column(String(36), nullable=False, unique=True, index=True)
    registered_at = Column(DateTime, nullable=False, default=utcnow)
    # Set when the user is banned (hold limit exceeded); NULL otherwise.
    banned_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # Usernames are unique case-insensitively.
        Index("ix_users_username_lower", func.lower(username), unique=True),
        # Never reuse ids of deleted users.
        {"sqlite_autoincrement": True},
    )


class _SubmissionBase(TimestampMixin, Base):
    __abstract__ = True

    # All declared_attr so the column order is id, user_id, submitted_string,
    # accuracy (plain mixin columns would be placed before/after them).
    @declared_attr
    def id(cls):
        return Column(Integer, primary_key=True)

    @declared_attr
    def user_id(cls):
        return Column(
            Integer,
            ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

    @declared_attr
    def submitted_string(cls):
        return Column(String(1024), nullable=False)

    @declared_attr
    def accuracy(cls):
        return Column(Float, nullable=False)

    @declared_attr
    def iteration(cls):
        # server_default also covers rows inserted outside the ORM.
        return Column(Integer, nullable=False, default=0, server_default="0")

    # Tile coordinate. Nullable only so historical rows (which never stored it)
    # survive migration; those rows are ignored for tile state but still count
    # for leaderboards.
    @declared_attr
    def x(cls):
        return Column(Integer, nullable=True)

    @declared_attr
    def y(cls):
        return Column(Integer, nullable=True)

    @declared_attr
    def submission_id(cls):
        return Column(String(36), nullable=True)

    @declared_attr
    def reference_iteration(cls):
        return Column(Integer, nullable=True)

    @declared_attr
    def context_sources(cls):
        return Column(Text, nullable=True)  # JSON list of region source iterations

    @declared_attr
    def time_taken(cls):
        return Column(Float, nullable=True)


class SubmittedForTypeOne(_SubmissionBase):
    __tablename__ = "submitted_for_type_one"
    __table_args__ = (
        Index("ux_type_one_tile", "iteration", "x", "y", unique=True),
        Index("ux_type_one_submission", "user_id", "submission_id", unique=True),
        Index("ix_type_one_xy_iteration", "x", "y", "iteration"),
    )


class SubmittedForTypeTwo(_SubmissionBase):
    __tablename__ = "submitted_for_type_two"
    __table_args__ = (
        Index("ux_type_two_tile", "iteration", "x", "y", unique=True),
        Index("ux_type_two_submission", "user_id", "submission_id", unique=True),
        Index("ix_type_two_xy_iteration", "x", "y", "iteration"),
    )


SUBMISSION_MODELS = {1: SubmittedForTypeOne, 2: SubmittedForTypeTwo}


class TileHold(TimestampMixin, Base):
    """A 40-second claim on a tile of a given iteration."""

    __tablename__ = "tile_holds"

    id = Column(Integer, primary_key=True)
    game_type = Column(Integer, nullable=False)
    iteration = Column(Integer, nullable=False)
    x = Column(Integer, nullable=False)
    y = Column(Integer, nullable=False)
    user_id = Column(
        Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    acquired_at = Column(DateTime, nullable=False, default=utcnow)
    expires_at = Column(DateTime, nullable=False)
    # Reference frozen at allocation time (backend_design.md section 5).
    reference_iteration = Column(Integer, nullable=False)
    context_sources = Column(Text, nullable=True)

    __table_args__ = (
        Index("ux_tile_holds_tile", "game_type", "iteration", "x", "y", unique=True),
    )


class SuspiciousEvent(TimestampMixin, Base):
    """Suspicious activity shown on the admin dashboard (see security.py)."""

    __tablename__ = "suspicious_events"

    id = Column(Integer, primary_key=True)
    event_type = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=False, default="warning")  # info/warning/critical
    client_ip = Column(String, index=True)
    # Not a foreign key: deleting a user must not erase the evidence.
    user_id = Column(Integer, index=True)
    details = Column(Text)  # JSON object


class BannedIp(TimestampMixin, Base):
    """IPs refused by the middleware for every non-admin /api request."""

    __tablename__ = "banned_ips"

    id = Column(Integer, primary_key=True)
    ip = Column(String, nullable=False, unique=True)
    reason = Column(String)


class GameState(TimestampMixin, Base):
    """Per game type: bumped whenever a submission changes visible state."""

    __tablename__ = "game_state"

    game_type = Column(Integer, primary_key=True, autoincrement=False)
    state_version = Column(Integer, nullable=False, default=0)
