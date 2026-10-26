"""Tiny schema upgrades for databases created before a column existed.

`create_all()` only creates missing tables, it never alters existing ones, so
new columns/indexes on existing tables are added here (idempotent, run at
startup).

Historical submission rows have no coordinates: their x/y stay NULL. They keep
counting for leaderboards but are ignored for tile state (NULLs never collide
in the unique indexes).
"""
from sqlalchemy import inspect, text

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


def add_column_if_missing(engine, table: str, column: str, ddl: str) -> None:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return  # create_all() will create it with the column already in place
    if column in {c["name"] for c in inspector.get_columns(table)}:
        return
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))


def run_migrations(engine) -> None:
    add_column_if_missing(engine, "users", "banned_at", "DATETIME")
    for table, short in SUBMISSION_TABLES.items():
        for column, ddl in SUBMISSION_COLUMNS:
            add_column_if_missing(engine, table, column, ddl)
        if table not in inspect(engine).get_table_names():
            continue
        with engine.begin() as conn:
            conn.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS ux_{short}_tile '
                f'ON "{table}" (iteration, x, y)'))
            conn.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS ux_{short}_submission '
                f'ON "{table}" (user_id, submission_id)'))
            conn.execute(text(
                f'CREATE INDEX IF NOT EXISTS ix_{short}_xy_iteration '
                f'ON "{table}" (x, y, iteration)'))
