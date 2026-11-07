import json
import os
import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# SQLite file used when DATABASE_URL is unset. Docker mounts ./data here.
DB_PATH = Path(os.environ.get("DB_PATH", ROOT_DIR / "data" / "app.db"))

# Supabase (or any Postgres) connection string. Blank counts as unset so an
# empty shell variable cannot pull the local stack off SQLite.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None


def postgres_schema() -> str:
    """Schema for Postgres only. Default `app` is not on Supabase's Data API."""
    raw = os.environ.get("DB_SCHEMA", "app").strip() or "app"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        raise RuntimeError(
            "DB_SCHEMA must be a plain identifier (letters, digits, underscore), "
            f"got {raw!r}"
        )
    return raw


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise RuntimeError(
        f"{name} must be 1, 0, true, false, yes, no, on, or off (got {raw!r})"
    )


# On by default so local docker keeps recording /api/admin/* into admin_requests.
# Set to 0 to stop. fly.toml turns this off for the hosted API.
LOG_ADMIN_REQUESTS = env_flag("LOG_ADMIN_REQUESTS", True)

# JSON file holding {"username": "...", "password": "..."} for the admin
# dashboard. Never commit it (see .gitignore); in Docker it is mounted read-only.
# Ignored when both ADMIN_USERNAME and ADMIN_PASSWORD are set (Fly secrets).
ADMIN_SECRET_FILE = Path(
    os.environ.get("ADMIN_SECRET_FILE", ROOT_DIR / "secrets" / "admin.json")
)

FRONTEND_DIR = ROOT_DIR / "frontend"

# Original image (iteration 0): 384 lines of 480 palette symbols. If absent,
# iteration 0 is a blank image.
ORIGINAL_IMAGE_FILE = Path(
    os.environ.get("ORIGINAL_IMAGE_FILE", ROOT_DIR / "image" / "original.txt")
)


def load_admin_credentials():
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if username or password:
        if not username or not password:
            raise RuntimeError(
                "Set both ADMIN_USERNAME and ADMIN_PASSWORD, or neither. "
                "Neither falls back to the admin secret file."
            )
        return username, password
    try:
        # utf-8-sig tolerates a BOM (Windows PowerShell likes to add one).
        with open(ADMIN_SECRET_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        username, password = str(data["username"]), str(data["password"])
    except FileNotFoundError:
        raise RuntimeError(
            f"Admin secret file not found: {ADMIN_SECRET_FILE}. "
            'Create it with {"username": "...", "password": "..."} '
            "(see secrets/admin.example.json), or set ADMIN_USERNAME and "
            "ADMIN_PASSWORD."
        )
    except (KeyError, ValueError) as e:
        raise RuntimeError(f"Invalid admin secret file {ADMIN_SECRET_FILE}: {e!r}")
    if not username or not password:
        raise RuntimeError("Admin username/password in the secret file must not be empty.")
    return username, password
