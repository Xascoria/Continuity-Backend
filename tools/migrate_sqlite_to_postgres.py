"""Copy data/app.db into Postgres (Supabase), preserving ids.

The local SQLite file is opened read-only and is not modified.

    python tools/migrate_sqlite_to_postgres.py --sqlite data/app.db --dry-run
    python tools/migrate_sqlite_to_postgres.py --sqlite data/app.db

DATABASE_URL or --postgres is the destination. Use the Supabase session
pooler (port 5432). Tables are created in the schema from DB_SCHEMA
(default `app`) and sequences are moved past the copied ids so the next
insert does not collide.

Refuses to write when the destination already has rows. --replace truncates
only this app's tables in that schema, then loads the snapshot.

Stop the API first (`.\\run.ps1 -Down`) so rows written during the copy are
not left behind.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Float, Integer, String, inspect, select, text
from sqlalchemy.sql import func

ROOT = Path(__file__).resolve().parent.parent
BATCH = 200


def _parse_dt(value):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1]
    # Drop a trailing offset such as +00:00. Python 3.9 fromisoformat
    # wants the separator to be "T", and SQLite stores a space.
    if len(raw) >= 6 and raw[-6] in "+-" and raw[-3] == ":":
        raw = raw[:-6]
    if " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T", 1)
    return datetime.fromisoformat(raw)


def convert_value(column, value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    kind = column.type
    if isinstance(value, str) and value == "" and not isinstance(kind, String):
        return None
    if isinstance(kind, DateTime):
        return _parse_dt(value)
    if isinstance(kind, Boolean):
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "t", "yes"}
    if isinstance(kind, Integer):
        return int(value)
    if isinstance(kind, Float):
        return float(value)
    if isinstance(kind, String) and kind.length and isinstance(value, str):
        if len(value) > kind.length:
            raise ValueError("length %s exceeds %s" % (len(value), kind.length))
    return value


def _sequenced_pk_columns(table):
    columns = []
    for column in table.primary_key.columns:
        # 'auto' (the usual integer primary key) and True both get a sequence.
        # game_state.game_type is autoincrement=False and must be skipped.
        if column.autoincrement is False:
            continue
        if isinstance(column.type, Integer) and not isinstance(column.type, Boolean):
            columns.append(column)
    return columns


def _qualified(table) -> str:
    schema = table.schema or table.metadata.schema
    if schema:
        return '"%s"."%s"' % (schema, table.name)
    return '"%s"' % table.name


def _open_sqlite(path: Path) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sqlite_tables(conn) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _sqlite_columns(conn, table_name: str) -> list:
    rows = conn.execute('PRAGMA table_info("%s")' % table_name).fetchall()
    return [row[1] for row in rows]


def _sqlite_count(conn, table_name: str) -> int:
    return conn.execute('SELECT COUNT(*) FROM "%s"' % table_name).fetchone()[0]


def _destination_counts(conn, tables) -> dict:
    schema = tables[0].metadata.schema if tables else None
    present = set(inspect(conn).get_table_names(schema=schema))
    counts = {}
    for table in tables:
        if table.name not in present:
            counts[table.name] = None
            continue
        counts[table.name] = conn.execute(
            select(func.count()).select_from(table)
        ).scalar_one()
    return counts


def _truncate(conn, tables) -> None:
    names = ", ".join(_qualified(table) for table in tables)
    conn.execute(text("TRUNCATE TABLE %s RESTART IDENTITY CASCADE" % names))


def _copy_table(pg_conn, sqlite_conn, table, columns) -> int:
    pk_names = [column.name for column in table.primary_key.columns if column.name in columns]
    order = ""
    if pk_names:
        order = " ORDER BY " + ", ".join('"%s"' % name for name in pk_names)
    selected = ", ".join('"%s"' % name for name in columns)
    cursor = sqlite_conn.execute(
        'SELECT %s FROM "%s"%s' % (selected, table.name, order)
    )
    copied = 0
    while True:
        batch = cursor.fetchmany(BATCH)
        if not batch:
            break
        payload = []
        for row in batch:
            item = {}
            for name in columns:
                column = table.columns[name]
                try:
                    item[name] = convert_value(column, row[name])
                except (TypeError, ValueError) as exc:
                    pk = row[pk_names[0]] if pk_names else "?"
                    raise SystemExit(
                        "%s row %s column %s: %s" % (table.name, pk, name, exc)
                    )
            payload.append(item)
        pg_conn.execute(table.insert(), payload)
        copied += len(payload)
    return copied


def _reset_sequences(conn, tables) -> None:
    for table in tables:
        for column in _sequenced_pk_columns(table):
            qualified_name = table.name
            if table.metadata.schema:
                qualified_name = "%s.%s" % (table.metadata.schema, table.name)
            seq = conn.execute(
                text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": qualified_name, "column_name": column.name},
            ).scalar()
            if not seq:
                raise SystemExit(
                    "No Postgres sequence for %s.%s. "
                    "Refusing to finish, because the next insert would reuse an id."
                    % (qualified_name, column.name)
                )
            max_id = conn.execute(
                text('SELECT MAX("%s") FROM %s' % (column.name, _qualified(table)))
            ).scalar()
            if max_id is None:
                conn.execute(
                    text("SELECT setval(CAST(:seq AS regclass), 1, false)"),
                    {"seq": seq},
                )
            else:
                conn.execute(
                    text("SELECT setval(CAST(:seq AS regclass), :max_id, true)"),
                    {"seq": seq, "max_id": int(max_id)},
                )


def _resolve_sqlite(raw: str) -> Path:
    path = Path(raw)
    if path.is_file():
        return path
    alt = ROOT / raw
    if alt.is_file():
        return alt
    raise SystemExit("SQLite file not found: %s" % raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite", default="data/app.db", help="Source SQLite file (default data/app.db)"
    )
    parser.add_argument(
        "--postgres",
        default=None,
        help="Destination URL. Defaults to the DATABASE_URL environment variable.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Truncate this app's tables in the destination schema, then load.",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="TABLE",
        help="Create this table but copy no rows into it. Repeat to skip more than one.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print row counts only. Creates the schema if needed, not the tables.",
    )
    args = parser.parse_args(argv)

    if args.postgres:
        os.environ["DATABASE_URL"] = args.postgres
    if not os.environ.get("DATABASE_URL", "").strip():
        parser.error("Pass --postgres or set DATABASE_URL")

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from backend.database import Base, describe_database, engine
    from backend.migrations import run_migrations
    import backend.models  # noqa: F401  (registers tables on Base)

    if engine.dialect.name != "postgresql":
        raise SystemExit("DATABASE_URL is not a Postgres URL: %s" % describe_database())

    source = _resolve_sqlite(args.sqlite)
    tables = list(Base.metadata.sorted_tables)
    skip = set(args.skip)
    unknown = skip - {table.name for table in tables}
    if unknown:
        raise SystemExit("Unknown --skip table(s): %s" % ", ".join(sorted(unknown)))
    print("Source:      %s" % source)
    print("Destination: %s" % describe_database())
    if skip:
        print("Not copying: %s" % ", ".join(sorted(skip)))

    sqlite_conn = _open_sqlite(source)
    try:
        present = _sqlite_tables(sqlite_conn)
        known = {table.name for table in tables}
        extra = sorted(present - known)
        if extra:
            print("Skipping SQLite tables that are not in the current schema: %s" % ", ".join(extra))

        print("%-28s %10s %10s" % ("table", "sqlite", "postgres"))
        if args.dry_run:
            with engine.connect() as pg_conn:
                dest = _destination_counts(pg_conn, tables)
            for table in tables:
                src = _sqlite_count(sqlite_conn, table.name) if table.name in present else 0
                if table.name in skip:
                    dest_n = "skip"
                elif dest[table.name] is None:
                    dest_n = "missing"
                else:
                    dest_n = dest[table.name]
                print("%-28s %10s %10s" % (table.name, src, dest_n))
            print("Dry run: no tables created, no rows copied.")
            return 0

        Base.metadata.create_all(bind=engine)
        run_migrations(engine)

        with engine.begin() as pg_conn:
            dest = _destination_counts(pg_conn, tables)
            occupied = [
                "%s=%s" % (name, count)
                for name, count in dest.items()
                if count and name not in skip
            ]
            if occupied and not args.replace:
                raise SystemExit(
                    "Destination already has rows (%s). "
                    "Re-run with --replace to wipe this app's tables and load this snapshot."
                    % ", ".join(occupied)
                )
            if args.replace:
                _truncate(pg_conn, tables)
                print("Truncated destination tables in schema %s." % (Base.metadata.schema or "public"))

            for table in tables:
                src = _sqlite_count(sqlite_conn, table.name) if table.name in present else 0
                if table.name in skip:
                    pg_conn.execute(table.delete())
                    print("%-28s %10s %10s" % (table.name, src, "skipped"))
                    continue
                if table.name not in present or src == 0:
                    print("%-28s %10s %10s" % (table.name, src, 0))
                    continue
                columns = [
                    name for name in _sqlite_columns(sqlite_conn, table.name)
                    if name in table.columns
                ]
                copied = _copy_table(pg_conn, sqlite_conn, table, columns)
                print("%-28s %10s %10s" % (table.name, src, copied))
                if copied != src:
                    raise SystemExit(
                        "%s: copied %s of %s rows" % (table.name, copied, src)
                    )

            _reset_sequences(pg_conn, tables)
            dest_after = _destination_counts(pg_conn, tables)
            for table in tables:
                src = _sqlite_count(sqlite_conn, table.name) if table.name in present else 0
                if table.name in skip:
                    if dest_after[table.name] not in (0, None):
                        raise SystemExit(
                            "%s: expected no copied rows, postgres has %s"
                            % (table.name, dest_after[table.name])
                        )
                    continue
                if dest_after[table.name] != src:
                    raise SystemExit(
                        "%s: sqlite has %s rows but postgres has %s"
                        % (table.name, src, dest_after[table.name])
                    )
        print("Copied. Sequences are set so the next insert uses a new id.")
        return 0
    finally:
        sqlite_conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
