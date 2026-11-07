import os
import unittest

# Don't let a shell DATABASE_URL (Supabase) attach this process to Postgres.
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DB_SCHEMA", None)

from sqlalchemy import create_engine, inspect, text

from .migrations import column_ddl, run_migrations


class MigrationTests(unittest.TestCase):
    def test_adds_iteration_to_existing_tables_once(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            for t in ("submitted_for_type_one", "submitted_for_type_two"):
                conn.execute(text(
                    f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, user_id INTEGER, "
                    "submitted_string VARCHAR(1024), accuracy FLOAT, created_at DATETIME)"))
                conn.execute(text(
                    f"INSERT INTO {t} (user_id, submitted_string, accuracy) VALUES (1, 'x', 0)"))

        run_migrations(engine)
        run_migrations(engine)  # idempotent

        for t in ("submitted_for_type_one", "submitted_for_type_two"):
            cols = [c["name"] for c in inspect(engine).get_columns(t)]
            for name in ("iteration", "x", "y", "submission_id", "reference_iteration",
                         "context_sources", "time_taken"):
                self.assertIn(name, cols)
            with engine.connect() as conn:  # existing rows get the default / NULL coords
                self.assertEqual(conn.execute(text(f"SELECT iteration FROM {t}")).scalar(), 0)
                self.assertIsNone(conn.execute(text(f"SELECT x FROM {t}")).scalar())
            index_names = {i["name"] for i in inspect(engine).get_indexes(t)}
            self.assertEqual(len(index_names & {n for n in index_names if n.startswith(("ux_", "ix_type"))}), 3)

    def test_adds_banned_at_to_users(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username VARCHAR)"))
        run_migrations(engine)
        self.assertIn("banned_at", [c["name"] for c in inspect(engine).get_columns("users")])

    def test_missing_tables_are_ignored(self):
        run_migrations(create_engine("sqlite://"))

    def test_datetime_ddl_follows_the_dialect(self):
        self.assertEqual(column_ddl(create_engine("sqlite://"), "DATETIME"), "DATETIME")

        class Postgres:
            class dialect:
                name = "postgresql"

        self.assertEqual(column_ddl(Postgres(), "DATETIME"), "TIMESTAMP")
        self.assertEqual(
            column_ddl(Postgres(), "INTEGER NOT NULL DEFAULT 0"),
            "INTEGER NOT NULL DEFAULT 0",
        )


if __name__ == "__main__":
    unittest.main()
