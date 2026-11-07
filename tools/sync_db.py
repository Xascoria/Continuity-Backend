"""Replace tables in one database with the rows from the other.

    pull   Supabase -> local data/app.db
    push   local data/app.db -> Supabase

The destination tables are emptied and reloaded. Ids are preserved, and the
next id on that side is set past the highest copied id.

The remote URL is DATABASE_URL, or secrets/hosted.json if that variable is unset.
The local file is data/app.db. Nothing here prints the URL or the password.

    .\\.venv\\Scripts\\python.exe tools\\sync_db.py pull --dry-run
    .\\.venv\\Scripts\\python.exe tools\\sync_db.py pull --skip admin_requests
    .\\.venv\\Scripts\\python.exe tools\\sync_db.py push --skip admin_requests

Stop the local API before pull (`.\\run.ps1 -Down`). pull writes data/app.db,
and the running container has that file open. push only reads the file, so
the container can stay up.

--only and --skip choose tables. A parent such as users is not replaced
unless its child tables are included too, because deleting a user cascades
to that user's submissions.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.sql.schema import MetaData

import migrate_sqlite_to_postgres as mig


def _load_database_url(secrets_file: Path) -> str:
    env = os.environ.get("DATABASE_URL", "").strip()
    if env:
        print("Remote: DATABASE_URL from the environment")
        return env
    if not secrets_file.is_file():
        raise SystemExit(
            "Set DATABASE_URL or create %s (see secrets/hosted.example.json)."
            % secrets_file
        )
    try:
        data = json.loads(secrets_file.read_text(encoding="utf-8"))
        url = str(data.get("database_url") or "").strip()
    except (OSError, ValueError) as exc:
        raise SystemExit("Could not read %s: %s" % (secrets_file, exc))
    if not url:
        raise SystemExit("database_url in %s is empty." % secrets_file)
    print("Remote: database_url from %s" % secrets_file)
    return url


def _sqlite_engine(path: Path):
    engine = create_engine("sqlite:///%s" % path.resolve().as_posix())

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _sqlite_tables(metadata):
    """Same tables as the app, without the Postgres schema name."""
    copy = MetaData()
    for table in metadata.sorted_tables:
        table.to_metadata(copy, schema=None)
    return list(copy.sorted_tables)


def _select_tables(tables, only, skip):
    names = {table.name for table in tables}
    unknown = (set(only) | set(skip)) - names
    if unknown:
        raise SystemExit("Unknown table(s): %s" % ", ".join(sorted(unknown)))
    if only and skip:
        raise SystemExit("Pass --only or --skip, not both.")
    if only:
        chosen = [table for table in tables if table.name in set(only)]
    else:
        chosen = [table for table in tables if table.name not in set(skip)]
    if not chosen:
        raise SystemExit("No tables selected.")
    chosen_names = {table.name for table in chosen}
    if chosen_names != names:
        blocked = []
        for table in tables:
            if table.name in chosen_names:
                continue
            for fk in table.foreign_key_constraints:
                parent = fk.referred_table.name if fk.referred_table is not None else ""
                if parent in chosen_names and (fk.ondelete or "").upper() == "CASCADE":
                    blocked.append("%s -> %s" % (parent, table.name))
        if blocked:
            raise SystemExit(
                "Refusing to replace a parent while leaving its child tables: %s. "
                "Include those tables, or sync every table."
                % ", ".join(blocked)
            )
    return chosen


def _ensure_local_writable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=1)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            "Local database is locked (%s). Stop the API first: .\\run.ps1 -Down" % exc
        )
    finally:
        conn.close()


def _reset_sqlite_sequences(conn, tables) -> None:
    exists = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")
    ).first()
    if exists is None:
        return
    for table in tables:
        columns = mig._sequenced_pk_columns(table)
        if len(columns) != 1:
            continue
        column = columns[0].name
        max_id = conn.execute(
            text('SELECT MAX("%s") FROM "%s"' % (column, table.name))
        ).scalar()
        if max_id is None:
            conn.execute(
                text("DELETE FROM sqlite_sequence WHERE name = :name"),
                {"name": table.name},
            )
            continue
        updated = conn.execute(
            text("UPDATE sqlite_sequence SET seq = :seq WHERE name = :name"),
            {"seq": int(max_id), "name": table.name},
        )
        if updated.rowcount == 0:
            conn.execute(
                text("INSERT INTO sqlite_sequence (name, seq) VALUES (:name, :seq)"),
                {"name": table.name, "seq": int(max_id)},
            )


def _copy_postgres_to_sqlite(pg_conn, sqlite_conn, pg_table, sqlite_table) -> int:
    columns = [column.name for column in pg_table.columns]
    pk_name = list(pg_table.primary_key.columns)[0].name if pg_table.primary_key else None
    copied = 0
    payload = []
    for row in pg_conn.execute(select(pg_table)):
        mapping = row._mapping
        item = {}
        pk = mapping.get(pk_name) if pk_name else "?"
        for name in columns:
            try:
                item[name] = mig.convert_value(pg_table.columns[name], mapping[name])
            except (TypeError, ValueError) as exc:
                raise SystemExit("%s row %s column %s: %s" % (pg_table.name, pk, name, exc))
        payload.append(item)
        if len(payload) >= mig.BATCH:
            sqlite_conn.execute(sqlite_table.insert(), payload)
            copied += len(payload)
            payload = []
    if payload:
        sqlite_conn.execute(sqlite_table.insert(), payload)
        copied += len(payload)
    return copied


def _print_counts(pairs) -> None:
    print("%-28s %12s %12s" % ("table", "source", "destination"))
    for name, src, dest in pairs:
        print("%-28s %12s %12s" % (name, src, dest))


def _add_common(parser) -> None:
    parser.add_argument("--sqlite", default="data/app.db", help="Local SQLite file (default data/app.db)")
    parser.add_argument(
        "--secrets",
        default="secrets/hosted.json",
        help="JSON file with database_url, used when DATABASE_URL is unset",
    )
    parser.add_argument("--only", action="append", default=[], metavar="TABLE", help="Sync just this table. Repeatable.")
    parser.add_argument("--skip", action="append", default=[], metavar="TABLE", help="Leave this table untouched. Repeatable.")
    parser.add_argument("--dry-run", action="store_true", help="Print row counts only.")


def _build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="direction", required=True)
    pull = commands.add_parser("pull", help="Replace local SQLite tables with Supabase rows")
    push = commands.add_parser("push", help="Replace Supabase tables with local SQLite rows")
    _add_common(pull)
    _add_common(push)
    return parser


def _resolve_sqlite(raw: str, create: bool) -> Path:
    path = Path(raw)
    if not path.is_file():
        alt = ROOT / raw
        if alt.is_file() or not path.is_absolute():
            path = alt
    if path.is_file() or create:
        return path
    raise SystemExit("SQLite file not found: %s" % raw)


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    secrets_file = Path(args.secrets)
    if not secrets_file.is_file():
        secrets_file = ROOT / args.secrets
    os.environ["DATABASE_URL"] = _load_database_url(secrets_file)

    from backend.database import Base, describe_database, engine
    from backend.migrations import run_migrations
    import backend.models  # noqa: F401

    if engine.dialect.name != "postgresql":
        raise SystemExit("Remote database is not Postgres: %s" % describe_database())

    sqlite_path = _resolve_sqlite(args.sqlite, create=args.direction == "pull" and not args.dry_run)
    pg_tables = list(Base.metadata.sorted_tables)
    selected = _select_tables(pg_tables, args.only, args.skip)

    print("Direction:   %s" % args.direction)
    print("Local:       %s" % sqlite_path)
    print("Remote:      %s" % describe_database())
    if len(selected) != len(pg_tables):
        print("Tables:      %s" % ", ".join(table.name for table in selected))

    if args.direction == "pull":
        return _pull(args, sqlite_path, engine, selected, run_migrations)
    return _push(args, sqlite_path, engine, Base, pg_tables, selected, run_migrations)


def _local_counts(sqlite_path: Path, selected) -> dict:
    counts = {table.name: 0 for table in selected}
    if not sqlite_path.is_file():
        return counts
    read = mig._open_sqlite(sqlite_path)
    try:
        present = mig._sqlite_tables(read)
        for table in selected:
            if table.name in present:
                counts[table.name] = mig._sqlite_count(read, table.name)
    finally:
        read.close()
    return counts


def _pull(args, sqlite_path, pg_engine, selected, run_migrations) -> int:
    local_counts = _local_counts(sqlite_path, selected)
    with pg_engine.connect() as pg_conn:
        remote = mig._destination_counts(pg_conn, selected)
    if args.dry_run:
        _print_counts([
            (
                table.name,
                "missing" if remote[table.name] is None else remote[table.name],
                local_counts[table.name],
            )
            for table in selected
        ])
        print("Dry run: local database was not changed.")
        return 0

    _ensure_local_writable(sqlite_path)
    sqlite_engine = _sqlite_engine(sqlite_path)
    local_tables = _sqlite_tables(selected[0].metadata)
    local_by_name = {table.name: table for table in local_tables}
    local_tables[0].metadata.create_all(sqlite_engine)
    # Models are bound to the Postgres schema. Point the column-adding
    # migration at this SQLite file for the duration of the call.
    from backend.database import Base
    saved_schema = Base.metadata.schema
    try:
        Base.metadata.schema = None
        run_migrations(sqlite_engine)
    finally:
        Base.metadata.schema = saved_schema

    with pg_engine.connect() as pg_conn:
        with sqlite_engine.begin() as sqlite_conn:
            for table in reversed(selected):
                sqlite_conn.execute(local_by_name[table.name].delete())
            for table in selected:
                copied = _copy_postgres_to_sqlite(
                    pg_conn, sqlite_conn, table, local_by_name[table.name]
                )
                print("%-28s %12s" % (table.name, copied))
            _reset_sqlite_sequences(sqlite_conn, selected)
    print("Pulled. Local tables now match the remote rows.")
    return 0


def _push(args, sqlite_path, pg_engine, Base, pg_tables, selected, run_migrations) -> int:
    if not sqlite_path.is_file():
        raise SystemExit("SQLite file not found: %s" % sqlite_path)
    read = mig._open_sqlite(sqlite_path)
    try:
        present = mig._sqlite_tables(read)
        local_counts = {
            table.name: mig._sqlite_count(read, table.name) if table.name in present else 0
            for table in selected
        }
        with pg_engine.connect() as pg_conn:
            remote = mig._destination_counts(pg_conn, selected)
        if args.dry_run:
            _print_counts([
                (
                    table.name,
                    local_counts[table.name],
                    "missing" if remote[table.name] is None else remote[table.name],
                )
                for table in selected
            ])
            print("Dry run: remote database was not changed.")
            return 0

        Base.metadata.create_all(bind=pg_engine)
        run_migrations(pg_engine)
        with pg_engine.begin() as pg_conn:
            if len(selected) == len(pg_tables):
                mig._truncate(pg_conn, pg_tables)
            else:
                for table in reversed(selected):
                    pg_conn.execute(table.delete())
            for table in selected:
                if table.name not in present or local_counts[table.name] == 0:
                    print("%-28s %12s" % (table.name, 0))
                    continue
                columns = [
                    name for name in mig._sqlite_columns(read, table.name)
                    if name in table.columns
                ]
                copied = mig._copy_table(pg_conn, read, table, columns)
                print("%-28s %12s" % (table.name, copied))
                if copied != local_counts[table.name]:
                    raise SystemExit(
                        "%s: copied %s of %s rows" % (table.name, copied, local_counts[table.name])
                    )
            mig._reset_sequences(pg_conn, selected)
    finally:
        read.close()
    print("Pushed. Remote tables now match the local rows.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
