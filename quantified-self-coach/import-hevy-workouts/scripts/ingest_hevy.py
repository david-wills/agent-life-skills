#!/usr/bin/env python3
"""Ingest a raw Hevy export JSON file into the local index.

Reads the JSON produced by `import_hevy_workouts.py`, writes per-workout
markdown files under `<data_root>/knowledge/hevy/<workout_id>.md`, and upserts
rows into the FTS5 index at `<data_root>/knowledge/index.db`.

Idempotent: re-ingesting the same workout updates the file and index row in place.
"""

from __future__ import annotations

import argparse
import os
import datetime as dt
import json
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
from fts_schema import ENTRIES_DDL as FTS_SCHEMA  # noqa: E402



WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _yaml_list(items: list[Any]) -> str:
    if not items:
        return "[]"
    parts = [_yaml_scalar(str(item)) for item in items]
    return "[" + ", ".join(parts) + "]"


def _frontmatter(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            lines.append(f"{key}: {_yaml_list(value)}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{key}: {value}")
        elif value is None or value == "":
            lines.append(f"{key}: \"\"")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _ensure_db(db_path: Path | None) -> sqlite3.Connection:
    if db_path is None:
        conn = sqlite3.connect(":memory:")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(FTS_SCHEMA)
    return conn


def _upsert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
    conn.execute(
        """
        INSERT INTO entries
            (id, source, source_type, title, author, url, readwise_url,
             captured_at, ingested_at, status, tags, note, body, path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"],
            row["source"],
            row["source_type"],
            row["title"] or "",
            row["author"] or "",
            row["url"] or "",
            row["readwise_url"] or "",
            row["captured_at"] or "",
            row["ingested_at"],
            row["status"],
            " ".join(row["tags"]),
            row["note"] or "",
            row["body"] or "",
            str(row["path"]),
        ),
    )


def _kg_to_lb(kg: float | int | None) -> float | None:
    if kg is None:
        return None
    return float(kg) * 2.20462


def _fmt_lb(kg: float | int | None) -> str:
    """Format weight as lb, rounded to nearest 0.5, no trailing .0."""
    lb = _kg_to_lb(kg)
    if lb is None:
        return ""
    rounded = round(lb * 2) / 2
    if rounded == int(rounded):
        return f"{int(rounded)}"
    return f"{rounded:g}"


def _fmt_duration(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    s = int(round(float(seconds)))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}:{s:02d}" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _fmt_distance_m(meters: int | float | None) -> str:
    if not meters:
        return ""
    km = float(meters) / 1000.0
    if km >= 0.1:
        return f"{km:.2f} km"
    return f"{int(round(float(meters)))} m"


def _fmt_set(s: dict[str, Any]) -> str:
    weight_kg = s.get("weight_kg")
    reps = s.get("reps")
    distance = s.get("distance_meters")
    duration = s.get("duration_seconds")
    rpe = s.get("rpe")

    parts: list[str] = []
    if weight_kg and reps:
        parts.append(f"{_fmt_lb(weight_kg)} lb × {reps}")
    elif reps and (weight_kg in (None, 0)):
        parts.append(f"BW × {reps}")
    elif weight_kg and not reps:
        parts.append(f"{_fmt_lb(weight_kg)} lb")
    if distance:
        parts.append(_fmt_distance_m(distance))
    if duration:
        parts.append(_fmt_duration(duration))
    if rpe is not None:
        parts.append(f"@ RPE {rpe}")
    if not parts:
        return "(empty set)"
    return " ".join(parts)


def _slug(value: str, max_len: int = 60) -> str:
    cleaned = []
    for ch in (value or "").lower():
        cleaned.append(ch if ch.isalnum() else "-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug or "untitled")[:max_len]


def _workout_volume_lb(workout: dict[str, Any]) -> tuple[float, int]:
    """Sum of weight_lb × reps across non-warmup sets with both. Returns (volume_lb, working_set_count)."""
    total = 0.0
    sets = 0
    for ex in workout.get("exercises") or []:
        for s in ex.get("sets") or []:
            if (s.get("type") or "").lower() == "warmup":
                continue
            wkg = s.get("weight_kg")
            reps = s.get("reps")
            if wkg and reps:
                total += float(wkg) * 2.20462 * int(reps)
                sets += 1
    return total, sets


def render_workout_markdown(workout: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (markdown_body_only, derived_meta) for a single workout dict."""
    title = (workout.get("title") or "Workout").strip()
    start = _parse_iso(workout.get("start_time") or "")
    end = _parse_iso(workout.get("end_time") or "")
    duration_min = None
    if start and end:
        duration_min = max(0, int(round((end - start).total_seconds() / 60)))

    weekday = WEEKDAYS[start.astimezone().weekday()] if start else ""
    date_str = start.astimezone().strftime("%Y-%m-%d") if start else ""

    volume_lb, working_sets = _workout_volume_lb(workout)
    exercises = workout.get("exercises") or []
    exercise_count = len(exercises)

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    meta_lines: list[str] = []
    if date_str:
        meta_lines.append(f"**Date:** {date_str}" + (f" ({weekday})" if weekday else ""))
    if duration_min is not None:
        meta_lines.append(f"**Duration:** {duration_min} min")
    if volume_lb > 0:
        meta_lines.append(f"**Working volume:** {int(round(volume_lb)):,} lb across {working_sets} set{'s' if working_sets != 1 else ''}")
    if exercise_count:
        meta_lines.append(f"**Exercises:** {exercise_count}")
    if workout.get("description"):
        meta_lines.append(f"**Notes:** {workout['description']}")
    lines.extend(meta_lines)
    lines.append("")

    if not exercises:
        lines.append("_No exercises logged. (Likely Apple Watch–only session, e.g. trainer day.)_")
    else:
        for ex in exercises:
            ex_title = (ex.get("title") or "Exercise").strip()
            lines.append(f"## {ex_title}")
            ex_notes = (ex.get("notes") or "").strip()
            if ex_notes:
                lines.append(f"_{ex_notes}_")
            for s in ex.get("sets") or []:
                set_type = (s.get("type") or "").lower()
                prefix = ""
                if set_type == "warmup":
                    prefix = "Warmup: "
                elif set_type == "failure":
                    prefix = "Failure: "
                elif set_type == "dropset":
                    prefix = "Drop: "
                lines.append(f"- {prefix}{_fmt_set(s)}")
            lines.append("")

    body = "\n".join(lines).rstrip() + "\n"

    derived = {
        "captured_at": workout.get("start_time") or workout.get("created_at") or "",
        "duration_min": duration_min,
        "weekday": weekday,
        "exercise_count": exercise_count,
        "working_set_count": working_sets,
        "working_volume_lb": int(round(volume_lb)) if volume_lb else 0,
        "tags": [_slug((ex.get("title") or "").strip()) for ex in exercises if ex.get("title")],
    }
    return body, derived


def ingest_export(
    export_path: Path,
    kb_root: Path,
    default_status: str = "confirmed",
    dry_run: bool = False,
) -> tuple[int, int]:
    """Ingest a single Hevy export JSON file. Returns (written, skipped)."""
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    workouts = payload.get("workouts") or []

    ingested_at = _iso_utc_now()
    out_root = kb_root / "hevy"
    if not dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
    conn = _ensure_db(None if dry_run else kb_root / "index.db")
    written = 0
    skipped = 0

    try:
        for w in workouts:
            wid = w.get("id")
            if not wid:
                skipped += 1
                continue

            body, derived = render_workout_markdown(w)
            md_path = out_root / f"{wid}.md"

            meta = {
                "id": f"hevy:{wid}",
                "source": "hevy",
                "source_type": "workout",
                "title": (w.get("title") or "Workout").strip(),
                "author": "",
                "url": "",
                "captured_at": derived["captured_at"],
                "ingested_at": ingested_at,
                "status": default_status,
                "weekday": derived["weekday"],
                "duration_min": derived["duration_min"] if derived["duration_min"] is not None else "",
                "exercise_count": derived["exercise_count"],
                "working_set_count": derived["working_set_count"],
                "working_volume_lb": derived["working_volume_lb"],
                "tags": derived["tags"],
            }

            doc = _frontmatter(meta) + "\n\n" + body
            if dry_run:
                print(f"would write {md_path.name}: {meta['title']} ({derived['weekday']}, {derived['exercise_count']} exercises)")
            else:
                md_path.write_text(doc.rstrip() + "\n", encoding="utf-8")

            _upsert(conn, {
                "id": meta["id"],
                "source": meta["source"],
                "source_type": meta["source_type"],
                "title": meta["title"],
                "author": "",
                "url": "",
                "readwise_url": "",
                "captured_at": meta["captured_at"],
                "ingested_at": meta["ingested_at"],
                "status": meta["status"],
                "tags": derived["tags"],
                "note": "",
                "body": body,
                "path": md_path,
            })
            written += 1
        conn.commit()
    finally:
        conn.close()

    return written, skipped


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("export", nargs="+", help="One or more raw Hevy export JSON files.")
    p.add_argument("--kb-root", default=None, help="Index root. Default: <data_root>/knowledge")
    p.add_argument("--status", default="confirmed", choices=["provisional", "confirmed"], help="Default status.")
    p.add_argument("--dry-run", action="store_true", help="List what would be written; write nothing.")
    return p.parse_args()


def main() -> int:
    os.umask(0o077)  # health data: every file this run creates is owner-only
    args = parse_args()
    if args.kb_root:
        kb_root = Path(args.kb_root).expanduser()
    else:
        from skill_config import data_root
        kb_root = data_root() / "knowledge"
    total_written = 0
    total_skipped = 0
    for export in args.export:
        export_path = Path(export).expanduser()
        if not export_path.is_file():
            print(f"skip: {export_path} is not a file", file=sys.stderr)
            continue
        written, skipped = ingest_export(export_path, kb_root=kb_root, default_status=args.status, dry_run=args.dry_run)
        print(f"{export_path}: ingested={written} skipped={skipped}")
        total_written += written
        total_skipped += skipped
    prefix = "dry-run " if args.dry_run else ""
    print(f"{prefix}Total: ingested={total_written} skipped={total_skipped} kb_root={kb_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
