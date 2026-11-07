"""Tiny schema upgrades for databases created before a column existed.

`create_all()` only creates missing tables, it never alters existing ones, so
new columns/indexes on existing tables are added here (idempotent, run at
startup).

Historical submission rows have no coordinates: their x/y stay NULL. They keep
counting for leaderboards but are ignored for tile state (NULLs never collide
in the unique indexes).
"""
from typing import Optional

from sqlalchemy import inspect, text

from .database import Base

SUBMISSION_TABLES = {
    "submitted_for_type_one": "type_one",
    "submitted_for_type_two": "type_two",
}

SUBMISSION_COLUMNS = [
    ("iteration", "INTEGER NOT NULL DEFAULT 0"),
    ("x", "INTEGER"),
    ("y", "INTEGER"),
    ("submission_id", "VARCHAR(36)"),
    ("reference_iteration", "INTEGER"),
    ("context_sources", "TEXT"),
    ("time_taken", "FLOAT"),
]


def column_ddl(engine, ddl: str) -> str:
    """SQLite understands DATETIME. Postgres wants TIMESTAMP."""
    if engine.dialect.name == "postgresql":
        return ddl.replace("DATETIME", "TIMESTAMP")
    return ddl


def _schema() -> Optional[str]:
    return Base.metadata.schema


def _qualified(table: str) -> str:
    schema = _schema()
    if schema:
        return f'"{schema}"."{table}"'
    return f'"{table}"'


def add_column_if_missing(engine, table: str, column: str, ddl: str) -> None:
    schema = _schema()
    inspector = inspect(engine)
    if table not in inspector.get_table_names(schema=schema):
        return  # create_all() will create it with the column already in place
    if column in {c["name"] for c in inspector.get_columns(table, schema=schema)}:
        return
    with engine.begin() as conn:
        conn.execute(text(
            f'ALTER TABLE {_qualified(table)} ADD COLUMN "{column}" {column_ddl(engine, ddl)}'
        ))


def run_migrations(engine) -> None:
    schema = _schema()
    add_column_if_missing(engine, "users", "banned_at", "DATETIME")
    for table, short in SUBMISSION_TABLES.items():
        for column, ddl in SUBMISSION_COLUMNS:
            add_column_if_missing(engine, table, column, ddl)
        if table not in inspect(engine).get_table_names(schema=schema):
            continue
        qualified = _qualified(table)
        with engine.begin() as conn:
            conn.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS ux_{short}_tile '
                f'ON {qualified} (iteration, x, y)'))
            conn.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS ux_{short}_submission '
                f'ON {qualified} (user_id, submission_id)'))
            conn.execute(text(
                f'CREATE INDEX IF NOT EXISTS ix_{short}_xy_iteration '
                f'ON {qualified} (x, y, iteration)'))
