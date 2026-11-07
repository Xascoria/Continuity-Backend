from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from .config import DATABASE_URL, DB_PATH, postgres_schema

Base = declarative_base()


def normalize_database_url(url: str) -> str:
    """Accept postgres:// and postgresql:// and use the psycopg 3 driver.

    Supabase refuses non-TLS connections, so a supabase.co host without
    sslmode gets sslmode=require. Other hosts are left alone (local Postgres).
    """
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and not url.startswith("postgresql+"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if "supabase.co" in url.lower() and "sslmode=" not in url.lower():
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def uses_transaction_pooler(url: str) -> bool:
    """Supabase transaction mode is port 6543. Session mode is port 5432."""
    lowered = url.lower()
    if "pgbouncer=true" in lowered:
        return True
    return ":6543/" in lowered or ":6543?" in lowered or lowered.endswith(":6543")


def _ensure_postgres_schema(url: str, schema: str) -> None:
    bootstrap = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with bootstrap.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    except Exception as exc:
        raise RuntimeError(
            f"Could not create Postgres schema {schema!r}. "
            "Check DATABASE_URL (use the Supabase session pooler, port 5432)."
        ) from exc
    finally:
        bootstrap.dispose()


def _make_engine():
    if not DATABASE_URL:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return create_engine(
            f"sqlite:///{DB_PATH}",
            connect_args={"check_same_thread": False},
        )

    url = normalize_database_url(DATABASE_URL)
    schema = postgres_schema()
    _ensure_postgres_schema(url, schema)

    # application_name shows up in Supabase's connection list.
    connect_args = {"connect_timeout": 10, "application_name": "20seconds"}
    kwargs = {"pool_pre_ping": True, "pool_recycle": 300}
    if uses_transaction_pooler(url):
        # Transaction-mode pgbouncer cannot reuse prepared statements, and
        # SQLAlchemy's own pool would pin server connections the pooler wants back.
        connect_args["prepare_threshold"] = None
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 2

    pg_engine = create_engine(url, connect_args=connect_args, **kwargs)

    @event.listens_for(pg_engine, "connect")
    def _prepare_postgres_connection(dbapi_connection, _record):
        # Tables are schema-qualified too. search_path covers any raw SQL.
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    return pg_engine


engine = _make_engine()

if DATABASE_URL:
    # Set before models import Base and define tables, so every table lands
    # in this schema. Unqualified names stay `users`, not `app.users`.
    Base.metadata.schema = postgres_schema()


if not DATABASE_URL:
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        # SQLite ignores foreign keys unless this is enabled per connection.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def describe_database() -> str:
    """Host and database only. Never the user or password."""
    if engine.dialect.name != "postgresql":
        return f"sqlite {DB_PATH}"
    url = engine.url
    schema = Base.metadata.schema or "public"
    return f"postgres {url.host}:{url.port}/{url.database} schema={schema}"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
