#!/usr/bin/env python3
"""Shared data layer for the restaurant assistant.

One SQLite database backs intake, planning, the reaction sweep and the day-of
brief. The DB is the product; the scripts are doors into it.

Everything writable lives under ``data_root()/restaurant-saver/`` (the database
and the Chrome profile). Nothing here reads config at import time, so
``--help`` works on a fresh clone with no config.json.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from skill_config import cfg, data_root  # noqa: E402

STATUS_WANT = "want_to_go"
STATUS_BEEN = "been"
STATUS_FAVORITE = "favorite"
STATUS_ARCHIVED = "archived"   # never delete a place; park it here instead

DATA_DIRNAME = "restaurant-saver"


def skill_data_dir() -> Path:
    """``<data_root>/restaurant-saver`` — database and browser profile live here."""
    d = data_root() / DATA_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return skill_data_dir() / "restaurants.db"


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    address               TEXT NOT NULL DEFAULT '',
    lat                   REAL,
    lng                   REAL,
    maps_url              TEXT,
    cuisine               TEXT,
    price                 TEXT,
    rating                TEXT,
    review_count          TEXT,
    note                  TEXT NOT NULL DEFAULT '',
    source_url            TEXT,
    source_raw            TEXT,
    drive_minutes         INTEGER,
    drive_text            TEXT,
    reservation_platform  TEXT,
    reservation_url       TEXT,
    status                TEXT NOT NULL DEFAULT 'want_to_go',
    added_at              TEXT NOT NULL,
    visited_at            TEXT,
    last_suggested_at     TEXT,
    UNIQUE(name, address)
);

CREATE TABLE IF NOT EXISTS suggestions (
    id             INTEGER PRIMARY KEY,
    restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
    target_date    TEXT NOT NULL,
    channel        TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'offered',
    posted_at      TEXT NOT NULL,
    resolved_at    TEXT,
    UNIQUE(message_id)
);

CREATE INDEX IF NOT EXISTS idx_restaurants_status ON restaurants(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_state  ON suggestions(state);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# browser identity
# --------------------------------------------------------------------------

# The resolver drives the real installed Google Chrome, not Playwright's bundled
# Chromium: Google refuses sign-in on the bundled build. macOS default below;
# set CHROME_PATH to point at a Chrome binary anywhere else.
DEFAULT_CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def chrome_bin() -> str:
    return os.environ.get("CHROME_PATH") or DEFAULT_CHROME_BIN


def chrome_version() -> str:
    """Major version of the installed Chrome, e.g. "140". Falls back to a recent one."""
    import subprocess
    try:
        out = subprocess.run([chrome_bin(), "--version"], capture_output=True,
                             text=True, timeout=10).stdout
        m = re.search(r"(\d+)\.", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "140"


def chrome_ua() -> str:
    """A UA string matching the installed Chrome.

    Chrome still sends real Sec-CH-UA client hints, so a hardcoded version that
    disagrees with them is an easy automation tell. Read the version live.
    """
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_version()}.0.0.0 Safari/537.36"
    )


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> dt.date:
    return dt.datetime.now().astimezone().date()


# --------------------------------------------------------------------------
# geo
# --------------------------------------------------------------------------

def coords_from_url(href: str) -> tuple[float | None, float | None]:
    """Extract (lat, lng) from a Google Maps place URL."""
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", href or "")
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in miles."""
    r = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def miles_from_home(lat: float | None, lng: float | None) -> float | None:
    if lat is None or lng is None:
        return None
    home = cfg("user.home")
    return haversine_miles(home["lat"], home["lng"], lat, lng)


# --------------------------------------------------------------------------
# reservation platform detection
# --------------------------------------------------------------------------

PLATFORM_PATTERNS = [
    ("resy",      re.compile(r"resy\.com", re.I)),
    ("opentable", re.compile(r"opentable\.com", re.I)),
    ("tock",      re.compile(r"exploretock\.com|tock\.com", re.I)),
    ("sevenrooms", re.compile(r"sevenrooms\.com", re.I)),
    ("yelp",      re.compile(r"yelp\.com/reservations", re.I)),
]


def platform_for_url(url: str | None) -> str | None:
    if not url:
        return None
    for name, pat in PLATFORM_PATTERNS:
        if pat.search(url):
            return name
    return "site"


# --------------------------------------------------------------------------
# restaurant rows
# --------------------------------------------------------------------------

FIELDS = (
    "name", "address", "lat", "lng", "maps_url", "cuisine", "price", "rating",
    "review_count", "note", "source_url", "source_raw", "drive_minutes",
    "drive_text", "reservation_platform", "reservation_url", "status", "visited_at",
)


def find_restaurant(conn: sqlite3.Connection, name: str, address: str = "") -> sqlite3.Row | None:
    """Locate a stored place by name (+ address when we have one).

    A name-only match is only trusted when one side has no address: two places
    sharing a name at different addresses are different restaurants, and a
    bare name lookup (the day-of brief) has nothing to compare against.
    """
    if address:
        row = conn.execute(
            "SELECT * FROM restaurants WHERE lower(name)=lower(?) AND lower(address)=lower(?)",
            (name, address),
        ).fetchone()
        if row:
            return row
        return conn.execute(
            "SELECT * FROM restaurants WHERE lower(name)=lower(?) AND address=''", (name,)
        ).fetchone()
    return conn.execute(
        "SELECT * FROM restaurants WHERE lower(name)=lower(?)", (name,)
    ).fetchone()


def upsert_restaurant(conn: sqlite3.Connection, data: dict[str, Any]) -> tuple[int, bool]:
    """Insert or update a restaurant. Returns (id, created).

    On update, only non-empty incoming values overwrite stored ones — a later
    bare mention of a place must never blank out its enrichment.
    """
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("restaurant name is required")
    address = (data.get("address") or "").strip()

    existing = find_restaurant(conn, name, address)
    if existing is None:
        cols = [f for f in FIELDS if f in data]
        payload = {c: data[c] for c in cols}
        payload.setdefault("status", STATUS_WANT)
        payload["added_at"] = now_iso()
        placeholders = ", ".join("?" for _ in payload)
        sql = f"INSERT INTO restaurants ({', '.join(payload)}) VALUES ({placeholders})"
        cur = conn.execute(sql, list(payload.values()))
        conn.commit()
        return int(cur.lastrowid), True

    updates = {}
    for f in FIELDS:
        if f not in data:
            continue
        new = data[f]
        if new in (None, "", 0):
            continue
        if f == "note" and existing["note"]:
            # append rather than clobber an earlier reason for saving
            if new.strip() and new.strip() not in existing["note"]:
                updates["note"] = f"{existing['note']}; {new.strip()}"
            continue
        updates[f] = new
    if updates:
        sql = f"UPDATE restaurants SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?"
        conn.execute(sql, [*updates.values(), existing["id"]])
        conn.commit()
    return int(existing["id"]), False


def set_status(conn: sqlite3.Connection, restaurant_id: int, status: str) -> None:
    """Change a place's status. Archiving is the only way a place leaves the list."""
    conn.execute("UPDATE restaurants SET status=? WHERE id=?", (status, restaurant_id))
    conn.commit()
