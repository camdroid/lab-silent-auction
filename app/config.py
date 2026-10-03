"""Settings, all read from environment variables (see .env.example)."""

import os
import re
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Everything lives under /t/<TRACKER_TOKEN>/, so the token is the only "password".
TRACKER_TOKEN = _env("TRACKER_TOKEN")

DATA_DIR = Path(_env("DATA_DIR", "data"))
TIMEZONE = _env("TZ", "America/Chicago")
REFRESH_SECONDS = int(_env("REFRESH_SECONDS", "20"))
MAX_PHOTO_MB = int(_env("MAX_PHOTO_MB", "100"))

# Google Sheets. Leave GOOGLE_SHEET_ID empty to run in local mode (SQLite + a lots CSV).
GOOGLE_SHEET_ID = _env("GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_FILE = _env("GOOGLE_CREDENTIALS_FILE", "/run/secrets/google-service-account.json")
BUNDLE_SHEET_NAME = _env("BUNDLE_SHEET_NAME")  # empty = find the tab whose header has the Lot ID column
TRACKER_SHEET_NAME = _env("TRACKER_SHEET_NAME", "Tracker")
FLAGS_SHEET_NAME = _env("FLAGS_SHEET_NAME", "Flags")

# Column headers in the bundle sheet.
COL_LOT_ID = _env("COL_LOT_ID", "Lot ID")
COL_TITLE = _env("COL_TITLE", "Lot Title")
COL_SUBTITLE = _env("COL_SUBTITLE", "Subtitle")
COL_DESCRIPTION = _env("COL_DESCRIPTION", "Lot Description")
COL_TYPE = _env("COL_TYPE", "Physical or Virtual")
COL_AUCTION_URL = _env("COL_AUCTION_URL", "32Auctions URL")  # optional column

# Where "Open in 32Auctions" goes when a lot has no URL of its own.
AUCTION_URL = _env("AUCTION_URL", "https://www.32auctions.com")

# Local mode only: lots come from this CSV (same headers as the bundle sheet).
LOTS_CSV = Path(_env("LOTS_CSV", str(DATA_DIR / "lots.csv")))
SAMPLE_LOTS_CSV = Path(__file__).resolve().parent.parent / "sample" / "lots.csv"


def check() -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", TRACKER_TOKEN):
        raise SystemExit(
            "TRACKER_TOKEN must be at least 16 letters, digits, '-' or '_'. "
            "Generate one with: openssl rand -hex 16"
        )
    if GOOGLE_SHEET_ID and not Path(GOOGLE_CREDENTIALS_FILE).is_file():
        raise SystemExit(f"GOOGLE_SHEET_ID is set but no credentials file at {GOOGLE_CREDENTIALS_FILE}")
