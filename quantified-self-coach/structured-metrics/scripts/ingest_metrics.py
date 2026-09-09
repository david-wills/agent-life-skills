#!/usr/bin/env python3
"""Structured-metrics ingest into ``<data_root>/knowledge/index.db``.

Populates four tables (plus one view) from per-source canonical files:

  metrics_daily       — daily numeric metrics, source-tagged
  workouts            — per-workout summary (Hevy + Apple Health)
  exercise_sets       — per-set rows from Hevy
  sleep_sessions      — per-night sleep (Oura)
  metrics_daily_resolved (VIEW) — Oura > Apple Health > Hevy priority for
                                  cross-source overlaps; consumers read this.

Source files stay canonical. SQL is regenerable; re-running is idempotent.

Entry points:
  ingest_apple_health_metrics(conn, kb_root, since=None)
  ingest_apple_health_workouts(conn, kb_root, since=None)
  ingest_oura_metrics(conn, kb_root, since=None)
  ingest_hevy_workouts(conn, kb_root, since=None, imports_root=None)

CLI:
  python3 ingest_metrics.py --all                         # full backfill
  python3 ingest_metrics.py --days 7                      # last 7 days
  python3 ingest_metrics.py --source apple-health --days 7
  python3 ingest_metrics.py --source hevy --all
  python3 ingest_metrics.py --all --dry-run               # counts only, in-memory DB
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
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

KG_TO_LB = 2.2046226218


# Cross-source canonical metric names. Oura keys collapse to AH names where
# they overlap (HRV, RHR), so `WHERE metric = 'heart_rate_variability'` returns
# both sources' rows in metrics_daily.
OURA_METRIC_MAP = {
    "hrv_avg": "heart_rate_variability",
    "resting_hr": "resting_heart_rate",
    "total_sleep_min": "total_sleep_min",
    "deep_sleep_min": "deep_sleep_min",
    "rem_sleep_min": "rem_sleep_min",
    "sleep_score": "sleep_score",
    "readiness_score": "readiness_score",
}

# Apple Health metric names that surface to metrics_daily. Mirrors CORE_METRICS
# in import-apple-health/scripts/parse_hae_payload.py. Identity mapping
# (raw key == canonical name) — kept here so this script doesn't have to import
# from a sibling skill. Skip-list metrics from SKILL.md (sleep_*, blood_oxygen,
# walking_*, etc.) are NOT in this list and never surface to SQL.
AH_CORE_METRICS = {
    # Tier 1
    "vo2_max",
    "resting_heart_rate",
    "heart_rate_variability",
    "respiratory_rate",
    "apple_exercise_time",
    "active_energy",
    # Tier 2
    "step_count",
    "time_in_daylight",
    "weight_body_mass",
    "body_fat_percentage",
    "mindful_minutes",
    # Tier 3
    "apple_stand_hour",
    "apple_stand_time",
    # Tier 4
    "environmental_audio_exposure",
    "headphone_audio_exposure",
}


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS metrics_daily (
  date TEXT NOT NULL,
  source TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL,
  unit TEXT,
  device_source TEXT,
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (date, source, metric)
);

CREATE TABLE IF NOT EXISTS workouts (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  date TEXT NOT NULL,
  duration_min REAL,
  type TEXT,
  title TEXT,
  total_volume_lb REAL,
  total_calories REAL,
  avg_hr_bpm REAL,
  max_hr_bpm REAL,
  min_hr_bpm REAL,
  exercise_count INTEGER,
  set_count INTEGER,
  notes TEXT,
  ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercise_sets (
  workout_id TEXT NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
  set_index INTEGER NOT NULL,
  exercise_slug TEXT NOT NULL,
  exercise_title TEXT,
  weight_lb REAL,
  reps INTEGER,
  rpe REAL,
  is_warmup INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workout_id, set_index)
);

CREATE TABLE IF NOT EXISTS sleep_sessions (
  date TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  total_min REAL,
  deep_min REAL,
  rem_min REAL,
  light_min REAL,
  awake_min REAL,
  efficiency_pct REAL,
  hrv_avg REAL,
  resting_hr REAL,
  score REAL,
  ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_daily_metric_date ON metrics_daily(metric, date);
CREATE INDEX IF NOT EXISTS idx_workouts_date ON workouts(date);
CREATE INDEX IF NOT EXISTS idx_workouts_source_date ON workouts(source, date);
CREATE INDEX IF NOT EXISTS idx_exercise_sets_slug ON exercise_sets(exercise_slug);

DROP VIEW IF EXISTS metrics_daily_resolved;
CREATE VIEW metrics_daily_resolved AS
SELECT date, metric, value, unit, source, device_source, ingested_at
FROM metrics_daily m1
WHERE source = (
  SELECT source FROM metrics_daily m2
  WHERE m2.date = m1.date AND m2.metric = m1.metric
  ORDER BY CASE source
    WHEN 'oura' THEN 1
    WHEN 'apple-health' THEN 2
    WHEN 'hevy' THEN 3
    ELSE 4
  END
  LIMIT 1
);
"""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_db(db_path: Path | None) -> sqlite3.Connection:
    """Open (or create) the index. ``None`` gives an in-memory DB for --dry-run."""
    if db_path is None:
        conn = sqlite3.connect(":memory:")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_DDL)
    conn.commit()
    return conn


_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _ymd_from_str(s: str | None) -> str | None:
    if not isinstance(s, str):
        return None
    m = _DATE_RE.match(s)
    return m.group(1) if m else None


def local_day(ts: str | None) -> str | None:
    """Calendar day of an ISO timestamp in this machine's local timezone.

    Hevy stores start_time in UTC; a 6 PM session west of Greenwich is already
    tomorrow in UTC. The markdown from ingest_hevy.py uses the local day, and
    `workouts.date` must agree with it. Falls back to the leading YYYY-MM-DD when
    the string will not parse.
    """
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return _ymd_from_str(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone().date().isoformat()


# INSERT OR REPLACE would delete-then-insert the workouts row, and with
# foreign_keys=ON that cascades into exercise_sets. Upsert in place instead so
# a re-ingest without raw set data leaves the existing sets alone.
_WORKOUT_UPSERT = """
INSERT INTO workouts
    (id, source, started_at, ended_at, date, duration_min, type, title,
     total_volume_lb, total_calories, avg_hr_bpm, max_hr_bpm, min_hr_bpm,
     exercise_count, set_count, notes, ingested_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    source=excluded.source, started_at=excluded.started_at, ended_at=excluded.ended_at,
    date=excluded.date, duration_min=excluded.duration_min, type=excluded.type,
    title=excluded.title, total_volume_lb=excluded.total_volume_lb,
    total_calories=excluded.total_calories, avg_hr_bpm=excluded.avg_hr_bpm,
    max_hr_bpm=excluded.max_hr_bpm, min_hr_bpm=excluded.min_hr_bpm,
    exercise_count=excluded.exercise_count, set_count=excluded.set_count,
    notes=excluded.notes, ingested_at=excluded.ingested_at
"""


def _slugify(s: str) -> str:
    """Slugify an exercise title: 'Hack Squat (Machine)' -> 'hack-squat-machine'."""
    s = s.lower()
    s = re.sub(r"[()\[\]{}]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (parsed-frontmatter-dict, body). Minimal YAML — doesn't pull PyYAML."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fm_text = m.group(1)
    body = m.group(2).lstrip()
    fm: dict[str, Any] = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # Strip surrounding quotes
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        # JSON arrays/booleans/numbers
        if v.startswith("["):
            try:
                fm[k] = json.loads(v)
                continue
            except json.JSONDecodeError:
                pass
        if v in ("true", "false"):
            fm[k] = v == "true"
            continue
        try:
            if "." in v:
                fm[k] = float(v)
            else:
                fm[k] = int(v)
            continue
        except ValueError:
            pass
        fm[k] = v
    return fm, body


def _within_window(date_str: str, since: dt.date | None) -> bool:
    if since is None:
        return True
    try:
        d = dt.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return False
    return d >= since


# ----------------------------------------------------------------------------
# Apple Health — metrics_daily
# ----------------------------------------------------------------------------


def _aggregate_ah_metric(points: list[dict[str, Any]]) -> tuple[float | None, str | None, str | None]:
    """Reduce HAE per-day point list to a (value, unit, device_source) tuple.

    Mirrors parse_hae_payload._summarize_metric_points logic for SQL purposes:
      - 1 point with qty: take qty
      - 1 point with Avg/Min/Max: take Avg
      - N points with qty: sum qtys
      - N points with Avg: arithmetic mean of Avgs (loses precision but matches
        what a daily-rollup consumer would expect — Oura beats Apple anyway).
    """
    if not points:
        return None, None, None
    units = next((p.get("units") for p in points if p.get("units")), None)
    sources = sorted({p.get("source") for p in points if p.get("source")})
    device_source = "|".join(s for s in sources if s) or None

    qtys = [p["qty"] for p in points if isinstance(p.get("qty"), (int, float))]
    if qtys:
        total = sum(qtys) if len(qtys) > 1 else qtys[0]
        return float(total), units, device_source

    avgs = [p["Avg"] for p in points if isinstance(p.get("Avg"), (int, float))]
    if avgs:
        avg = sum(avgs) / len(avgs)
        return float(avg), units, device_source

    return None, units, device_source


def ingest_apple_health_metrics(
    conn: sqlite3.Connection, kb_root: Path, since: dt.date | None = None
) -> tuple[int, int]:
    sidecar_dir = kb_root / "apple-health" / ".sidecar"
    if not sidecar_dir.is_dir():
        return 0, 0

    ingested_at = _iso_utc_now()
    written = 0
    skipped = 0

    rows: list[tuple[Any, ...]] = []
    for f in sorted(sidecar_dir.glob("*.json")):
        date_str = f.stem
        if not _DATE_RE.match(date_str):
            skipped += 1
            continue
        if not _within_window(date_str, since):
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        metrics = payload.get("metrics") or {}
        for name, points in metrics.items():
            if name not in AH_CORE_METRICS:
                continue
            value, unit, device_source = _aggregate_ah_metric(points)
            if value is None:
                continue
            rows.append((date_str, "apple-health", name, value, unit, device_source, ingested_at))

    if rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO metrics_daily
                (date, source, metric, value, unit, device_source, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        written = len(rows)
    return written, skipped


# ----------------------------------------------------------------------------
# Apple Health — workouts
# ----------------------------------------------------------------------------


def _ah_workout_type(name: str | None) -> str | None:
    if not name:
        return None
    return _slugify(name)


def ingest_apple_health_workouts(
    conn: sqlite3.Connection, kb_root: Path, since: dt.date | None = None
) -> tuple[int, int]:
    sidecar_dir = kb_root / "apple-health" / ".sidecar"
    if not sidecar_dir.is_dir():
        return 0, 0

    ingested_at = _iso_utc_now()
    rows: list[tuple[Any, ...]] = []
    skipped = 0

    for f in sorted(sidecar_dir.glob("*.json")):
        date_str = f.stem
        if not _DATE_RE.match(date_str):
            skipped += 1
            continue
        if not _within_window(date_str, since):
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped += 1
            continue
        workouts = payload.get("workouts") or []
        for w in workouts:
            wid = w.get("id")
            if not wid:
                skipped += 1
                continue
            start = w.get("start") or ""
            end = w.get("end")
            wdate = _ymd_from_str(start) or date_str
            duration_s = w.get("duration") or 0
            duration_min = round(float(duration_s) / 60.0, 2) if duration_s else None
            hr = w.get("heartRate") or {}
            avg_hr = (hr.get("avg") or {}).get("qty")
            min_hr = (hr.get("min") or {}).get("qty")
            max_hr = (hr.get("max") or {}).get("qty")
            energy = (w.get("activeEnergyBurned") or {}).get("qty")
            name = w.get("name") or "Workout"
            rows.append((
                f"apple-health:{wid}",       # id
                "apple-health",              # source
                start or wdate,              # started_at
                end,                          # ended_at
                wdate,                        # date
                duration_min,                 # duration_min
                _ah_workout_type(name),       # type
                name,                         # title
                None,                         # total_volume_lb
                float(energy) if energy is not None else None,  # total_calories
                float(avg_hr) if avg_hr is not None else None,
                float(max_hr) if max_hr is not None else None,
                float(min_hr) if min_hr is not None else None,
                None,                         # exercise_count
                None,                         # set_count
                None,                         # notes
                ingested_at,
            ))

    if rows:
        conn.executemany(_WORKOUT_UPSERT, rows)
        conn.commit()
    return len(rows), skipped


# ----------------------------------------------------------------------------
# Oura — metrics_daily + sleep_sessions
# ----------------------------------------------------------------------------


def ingest_oura_metrics(
    conn: sqlite3.Connection, kb_root: Path, since: dt.date | None = None
) -> tuple[int, int, int]:
    """Returns (metrics_written, sleep_written, skipped)."""
    src_dir = kb_root / "oura"
    if not src_dir.is_dir():
        return 0, 0, 0

    ingested_at = _iso_utc_now()
    metric_rows: list[tuple[Any, ...]] = []
    sleep_rows: list[tuple[Any, ...]] = []
    skipped = 0

    for f in sorted(src_dir.glob("daily-*.md")):
        date_match = re.match(r"^daily-(\d{4}-\d{2}-\d{2})$", f.stem)
        if not date_match:
            skipped += 1
            continue
        date_str = date_match.group(1)
        if not _within_window(date_str, since):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            skipped += 1
            continue
        fm, _body = _split_frontmatter(text)
        day = fm.get("day") or date_str

        # metrics_daily rows
        for raw_key, canonical in OURA_METRIC_MAP.items():
            v = fm.get(raw_key)
            if v is None or not isinstance(v, (int, float)):
                continue
            unit = _oura_unit_for(canonical)
            metric_rows.append((day, "oura", canonical, float(v), unit, "Oura Ring", ingested_at))

        # sleep_sessions row (one per night)
        sleep_rows.append((
            day,                                       # date
            "oura",                                    # source
            None,                                       # started_at (in body prose)
            None,                                       # ended_at (in body prose)
            _num(fm.get("total_sleep_min")),
            _num(fm.get("deep_sleep_min")),
            _num(fm.get("rem_sleep_min")),
            None,                                       # light_min (in body prose)
            None,                                       # awake_min (in body prose)
            None,                                       # efficiency_pct (in body prose)
            _num(fm.get("hrv_avg")),
            _num(fm.get("resting_hr")),
            _num(fm.get("sleep_score")),
            ingested_at,
        ))

    if metric_rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO metrics_daily
                (date, source, metric, value, unit, device_source, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            metric_rows,
        )
    if sleep_rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO sleep_sessions
                (date, source, started_at, ended_at, total_min, deep_min, rem_min,
                 light_min, awake_min, efficiency_pct, hrv_avg, resting_hr, score, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sleep_rows,
        )
    conn.commit()
    return len(metric_rows), len(sleep_rows), skipped


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _oura_unit_for(canonical: str) -> str | None:
    return {
        "heart_rate_variability": "ms",
        "resting_heart_rate": "count/min",
        "total_sleep_min": "min",
        "deep_sleep_min": "min",
        "rem_sleep_min": "min",
        "sleep_score": None,
        "readiness_score": None,
    }.get(canonical)


# ----------------------------------------------------------------------------
# Hevy — workouts + exercise_sets + metrics_daily volume rollup
# ----------------------------------------------------------------------------


def _latest_hevy_imports(imports_root: Path) -> list[Path]:
    """Return Hevy raw JSON files, newest-first.

    The full-history payloads are dropped here by the import-hevy-workouts
    skill. The most recent one is the source of truth for the per-set data;
    older files are kept as historical snapshots and may have more or fewer
    workouts depending on import mode.
    """
    d = imports_root / "hevy"
    if not d.is_dir():
        return []
    return sorted(d.glob("hevy-workouts-*.json"), reverse=True)


def _load_hevy_set_data(imports_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Build a {workout_id: [exercises]} map from Hevy raw imports.

    Walks newest-first, keeping the first encountered workout (so backfill or
    incremental imports later don't overwrite richer earlier dumps). Returns
    raw exercises[] with sets nested.
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    for path in _latest_hevy_imports(imports_root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for w in payload.get("workouts") or []:
            wid = w.get("id")
            if not wid or wid in by_id:
                continue
            exercises = w.get("exercises") or []
            by_id[wid] = exercises
    return by_id


def ingest_hevy_workouts(
    conn: sqlite3.Connection,
    kb_root: Path,
    since: dt.date | None = None,
    imports_root: Path | None = None,
) -> tuple[int, int, int, int]:
    """Returns (workouts_written, sets_written, volume_metric_rows, skipped)."""
    src_dir = kb_root / "hevy"
    if not src_dir.is_dir():
        return 0, 0, 0, 0

    set_data_by_id = _load_hevy_set_data(imports_root) if imports_root else {}

    ingested_at = _iso_utc_now()
    workout_rows: list[tuple[Any, ...]] = []
    set_rows: list[tuple[Any, ...]] = []
    ids_with_raw_sets: list[str] = []
    volume_by_date: dict[str, float] = {}
    skipped = 0

    for f in sorted(src_dir.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            skipped += 1
            continue
        fm, body = _split_frontmatter(text)
        raw_id = fm.get("id") or ""
        # frontmatter id is "hevy:<uuid>"; raw uuid is filename stem
        uuid = f.stem
        captured_at = fm.get("captured_at") or ""
        date_str = local_day(captured_at) or _ymd_from_str(raw_id) or ""
        if not date_str:
            skipped += 1
            continue
        if not _within_window(date_str, since):
            continue

        title = fm.get("title") or ""
        duration_min = _num(fm.get("duration_min"))
        exercise_count = fm.get("exercise_count")
        if isinstance(exercise_count, str):
            try:
                exercise_count = int(exercise_count)
            except ValueError:
                exercise_count = None
        set_count = fm.get("working_set_count")
        if isinstance(set_count, str):
            try:
                set_count = int(set_count)
            except ValueError:
                set_count = None
        volume_lb = _num(fm.get("working_volume_lb"))

        workout_rows.append((
            f"hevy:{uuid}",        # id
            "hevy",                # source
            captured_at,           # started_at
            None,                   # ended_at
            date_str,              # date
            duration_min,
            "lifting",
            title,
            volume_lb,
            None,                   # total_calories
            None, None, None,       # HR fields
            int(exercise_count) if isinstance(exercise_count, int) else None,
            int(set_count) if isinstance(set_count, int) else None,
            body or None,
            ingested_at,
        ))

        if volume_lb is not None:
            volume_by_date[date_str] = volume_by_date.get(date_str, 0.0) + volume_lb

        # Set-level rows from raw JSON. Only when this workout is present in a
        # raw import do we clear and rewrite its sets; otherwise whatever was
        # ingested before stays.
        if uuid not in set_data_by_id:
            continue
        ids_with_raw_sets.append(f"hevy:{uuid}")
        exercises = set_data_by_id[uuid] or []
        flat_index = 0
        for ex in exercises:
            ex_title = ex.get("title") or ""
            ex_slug = _slugify(ex_title) if ex_title else "unknown"
            for s in ex.get("sets") or []:
                weight_kg = s.get("weight_kg")
                weight_lb = (
                    round(float(weight_kg) * KG_TO_LB, 2)
                    if isinstance(weight_kg, (int, float))
                    else None
                )
                reps = s.get("reps")
                rpe = s.get("rpe")
                set_type = (s.get("type") or "").lower()
                is_warmup = 1 if set_type == "warmup" else 0
                set_rows.append((
                    f"hevy:{uuid}",
                    flat_index,
                    ex_slug,
                    ex_title,
                    weight_lb,
                    int(reps) if isinstance(reps, (int, float)) else None,
                    float(rpe) if isinstance(rpe, (int, float)) else None,
                    is_warmup,
                ))
                flat_index += 1

    # Apply per source: workouts (upsert in place) → exercise_sets (clear + rewrite
    # only for workouts whose raw JSON was found) → volume rollup
    if workout_rows:
        conn.executemany(_WORKOUT_UPSERT, workout_rows)
    if ids_with_raw_sets:
        conn.executemany("DELETE FROM exercise_sets WHERE workout_id = ?", [(i,) for i in ids_with_raw_sets])

    if set_rows:
        conn.executemany(
            """
            INSERT INTO exercise_sets
                (workout_id, set_index, exercise_slug, exercise_title,
                 weight_lb, reps, rpe, is_warmup)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            set_rows,
        )

    volume_rows = [
        (date_str, "hevy", "training_volume_lb", round(total, 2), "lb", "Hevy", ingested_at)
        for date_str, total in volume_by_date.items()
    ]
    if volume_rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO metrics_daily
                (date, source, metric, value, unit, device_source, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            volume_rows,
        )

    conn.commit()
    return len(workout_rows), len(set_rows), len(volume_rows), skipped


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kb-root", default=None, help="Index root. Default: <data_root>/knowledge")
    p.add_argument("--imports-root", default=None,
                   help="Where raw Hevy exports live (for exercise_sets). Default: <data_root>/imports")
    p.add_argument(
        "--source",
        choices=["apple-health", "oura", "hevy", "all"],
        default="all",
    )
    p.add_argument("--days", type=int, help="Only process the last N days.")
    p.add_argument("--since", help="Only process dates on or after YYYY-MM-DD.")
    p.add_argument(
        "--all", action="store_true", help="Backfill — ignore window."
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Compute and print the per-source counts against an in-memory DB; write nothing.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.kb_root and args.imports_root:
        kb_root = Path(args.kb_root).expanduser()
        imports_root = Path(args.imports_root).expanduser()
    else:
        from skill_config import data_root
        root = data_root()
        kb_root = Path(args.kb_root).expanduser() if args.kb_root else root / "knowledge"
        imports_root = Path(args.imports_root).expanduser() if args.imports_root else root / "imports"

    since: dt.date | None = None
    if args.all:
        since = None
    elif args.days is not None:
        since = dt.date.today() - dt.timedelta(days=args.days)
    elif args.since:
        try:
            since = dt.date.fromisoformat(args.since)
        except ValueError:
            print(f"--since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 2
    else:
        # Default for cron: last 7 days.
        since = dt.date.today() - dt.timedelta(days=7)

    conn = ensure_db(None if args.dry_run else kb_root / "index.db")
    prefix = "dry-run " if args.dry_run else ""
    try:
        if args.source in ("apple-health", "all"):
            m_w, m_sk = ingest_apple_health_metrics(conn, kb_root, since)
            w_w, w_sk = ingest_apple_health_workouts(conn, kb_root, since)
            print(f"{prefix}apple-health: metrics={m_w} workouts={w_w} skipped={m_sk + w_sk}")
        if args.source in ("oura", "all"):
            o_m, o_s, o_sk = ingest_oura_metrics(conn, kb_root, since)
            print(f"{prefix}oura: metrics={o_m} sleep_sessions={o_s} skipped={o_sk}")
        if args.source in ("hevy", "all"):
            h_w, h_s, h_v, h_sk = ingest_hevy_workouts(conn, kb_root, since, imports_root)
            print(f"{prefix}hevy: workouts={h_w} sets={h_s} volume_rows={h_v} skipped={h_sk}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
