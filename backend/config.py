import json
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

# SQLite database file. In Docker this points at the mounted ./data volume.
DB_PATH = Path(os.environ.get("DB_PATH", ROOT_DIR / "data" / "app.db"))

# JSON file holding {"username": "...", "password": "..."} for the admin
# dashboard. Never commit it (see .gitignore); in Docker it is mounted read-only.
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
    try:
        # utf-8-sig tolerates a BOM (Windows PowerShell likes to add one).
        with open(ADMIN_SECRET_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        username, password = str(data["username"]), str(data["password"])
    except FileNotFoundError:
        raise RuntimeError(
            f"Admin secret file not found: {ADMIN_SECRET_FILE}. "
            'Create it with {"username": "...", "password": "..."} '
            "(see secrets/admin.example.json)."
        )
    except (KeyError, ValueError) as e:
        raise RuntimeError(f"Invalid admin secret file {ADMIN_SECRET_FILE}: {e!r}")
    if not username or not password:
        raise RuntimeError("Admin username/password in the secret file must not be empty.")
    return username, password


ADMIN_USERNAME, ADMIN_PASSWORD = load_admin_credentials()
