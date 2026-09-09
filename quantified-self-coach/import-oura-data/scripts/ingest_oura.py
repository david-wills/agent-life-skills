#!/usr/bin/env python3
"""Ingest a raw Oura export JSON file into the knowledge base.

Reads the JSON produced by `skills/import-oura-data/scripts/import_oura_data.py`,
writes two flavors of markdown under `knowledge/oura/`:

  - workout-<id>.md   — one per workout event (cardio, walks, lifts, etc.)
  - daily-<date>.md   — one per day, combining readiness + daily sleep score
                        + main long_sleep session (HRV, RHR, total sleep, etc.)

Both are upserted into the FTS5 index at `knowledge/index.db` under source="oura".

Idempotent: re-ingesting the same export updates files and index rows in place.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


DEFAULT_KB_ROOT = Path.home() / ".openclaw" / "workspace" / "knowledge"


FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(
    id UNINDEXED,
    source UNINDEXED,
    source_type UNINDEXED,
    title,
    author,
    url UNINDEXED,
    readwise_url UNINDEXED,
    captured_at UNINDEXED,
    ingested_at UNINDEXED,
    status UNINDEXED,
    tags,
    note,
    body,
    path UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _iso_utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> dt.datetime | None:
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


def _parse_day(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        return None


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


def _ensure_db(db_path: Path) -> sqlite3.Connection:
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


def _slug(value: str, max_len: int = 60) -> str:
    cleaned = []
    for ch in (value or "").lower():
        cleaned.append(ch if ch.isalnum() else "-")
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug or "untitled")[:max_len]


def _fmt_duration_min(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    s = int(round(float(seconds)))
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _seconds_to_min(seconds: int | float | None) -> int | None:
    if not seconds:
        return None
    return int(round(float(seconds) / 60.0))


def _fmt_distance(meters: int | float | None) -> str:
    if not meters:
        return ""
    km = float(meters) / 1000.0
    if km >= 0.1:
        return f"{km:.2f} km"
    return f"{int(round(float(meters)))} m"


# ---------- workout ----------


def render_workout_markdown(w: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    activity = (w.get("activity") or "workout").strip()
    start = _parse_iso(w.get("start_datetime"))
    end = _parse_iso(w.get("end_datetime"))
    day = _parse_day(w.get("day"))
    weekday = WEEKDAYS[day.weekday()] if day else (WEEKDAYS[start.weekday()] if start else "")
    date_str = day.isoformat() if day else (start.astimezone().strftime("%Y-%m-%d") if start else "")

    duration_min: int | None = None
    if start and end:
        duration_min = max(0, int(round((end - start).total_seconds() / 60)))

    intensity = (w.get("intensity") or "").strip()
    distance_m = w.get("distance")
    calories = w.get("calories")
    data_source = (w.get("source") or "").strip()
    label = (w.get("label") or "").strip()

    title_bits = [activity.replace("_", " ").title()]
    if duration_min is not None:
        title_bits.append(f"— {duration_min} min")
    title = " ".join(title_bits).strip()

    lines: list[str] = [f"# {title}", ""]
    if date_str:
        lines.append(f"**Date:** {date_str}" + (f" ({weekday})" if weekday else ""))
    if start and end:
        lines.append(
            f"**Time:** {start.astimezone().strftime('%H:%M')} → "
            f"{end.astimezone().strftime('%H:%M')}"
            + (f" ({duration_min} min)" if duration_min is not None else "")
        )
    bits: list[str] = []
    if activity:
        bits.append(activity.replace("_", " "))
    if intensity:
        bits.append(f"{intensity} intensity")
    if bits:
        lines.append(f"**Activity:** {', '.join(bits)}")
    if distance_m:
        lines.append(f"**Distance:** {_fmt_distance(distance_m)}")
    if calories is not None:
        lines.append(f"**Calories:** {int(round(float(calories)))}")
    if data_source:
        lines.append(f"**Source:** {data_source}")
    if label:
        lines.append(f"**Label:** {label}")

    body = "\n".join(lines).rstrip() + "\n"

    tags: list[str] = []
    if activity:
        tags.append(_slug(activity))
    if intensity:
        tags.append(_slug(intensity))

    derived = {
        "captured_at": w.get("start_datetime") or "",
        "day": date_str,
        "weekday": weekday,
        "activity": activity,
        "intensity": intensity,
        "duration_min": duration_min if duration_min is not None else "",
        "distance_m": int(round(float(distance_m))) if distance_m else "",
        "calories": int(round(float(calories))) if calories is not None else "",
        "data_source": data_source,
        "tags": tags,
    }
    return body, derived


# ---------- daily (readiness + sleep) ----------


def _index_by_day(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        day = (item.get("day") or "").strip()
        if not day:
            continue
        out[day] = item
    return out


def _pick_main_sleep(sleep_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pick the longest long_sleep session per day; fall back to any longest."""
    by_day: dict[str, dict[str, Any]] = {}
    for s in sleep_items:
        day = (s.get("day") or "").strip()
        if not day:
            continue
        is_long = (s.get("type") or "").lower() == "long_sleep"
        cur = by_day.get(day)
        if cur is None:
            by_day[day] = s
            continue
        cur_long = (cur.get("type") or "").lower() == "long_sleep"
        if is_long and not cur_long:
            by_day[day] = s
            continue
        if is_long == cur_long:
            cur_dur = float(cur.get("total_sleep_duration") or 0)
            new_dur = float(s.get("total_sleep_duration") or 0)
            if new_dur > cur_dur:
                by_day[day] = s
    return by_day


def render_daily_markdown(
    day: str,
    readiness: dict[str, Any] | None,
    daily_sleep: dict[str, Any] | None,
    sleep_session: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    day_date = _parse_day(day)
    weekday = WEEKDAYS[day_date.weekday()] if day_date else ""
    title = f"Oura — {day}" + (f" ({weekday})" if weekday else "")

    readiness_score = readiness.get("score") if readiness else None
    sleep_score = daily_sleep.get("score") if daily_sleep else None

    total_sleep_min = _seconds_to_min(
        (sleep_session or {}).get("total_sleep_duration")
    )
    deep_sleep_min = _seconds_to_min((sleep_session or {}).get("deep_sleep_duration"))
    rem_sleep_min = _seconds_to_min((sleep_session or {}).get("rem_sleep_duration"))
    light_sleep_min = _seconds_to_min((sleep_session or {}).get("light_sleep_duration"))
    time_in_bed_min = _seconds_to_min((sleep_session or {}).get("time_in_bed"))
    awake_min = _seconds_to_min((sleep_session or {}).get("awake_time"))

    avg_hrv = (sleep_session or {}).get("average_hrv")
    avg_hr = (sleep_session or {}).get("average_heart_rate")
    lowest_hr = (sleep_session or {}).get("lowest_heart_rate")
    efficiency = (sleep_session or {}).get("efficiency")
    avg_breath = (sleep_session or {}).get("average_breath")
    bedtime_start = _parse_iso((sleep_session or {}).get("bedtime_start"))
    bedtime_end = _parse_iso((sleep_session or {}).get("bedtime_end"))

    lines: list[str] = [f"# {title}", ""]

    if readiness:
        lines.append(f"**Readiness:** {readiness_score}/100")
        contrib = readiness.get("contributors") or {}
        if contrib:
            labels = {
                "hrv_balance": "HRV balance",
                "sleep_balance": "Sleep balance",
                "resting_heart_rate": "Resting HR",
                "body_temperature": "Body temperature",
                "recovery_index": "Recovery index",
                "previous_day_activity": "Previous day activity",
                "previous_night": "Previous night",
                "activity_balance": "Activity balance",
            }
            for key, pretty in labels.items():
                if key in contrib and contrib[key] is not None:
                    lines.append(f"- {pretty}: {contrib[key]}")
        temp_dev = readiness.get("temperature_deviation")
        temp_trend = readiness.get("temperature_trend_deviation")
        if temp_dev is not None or temp_trend is not None:
            parts = []
            if temp_dev is not None:
                parts.append(f"deviation {temp_dev:+.2f}°")
            if temp_trend is not None:
                parts.append(f"trend {temp_trend:+.2f}°")
            lines.append(f"- Body temp: {', '.join(parts)}")
        lines.append("")

    if daily_sleep or sleep_session:
        sleep_header = f"**Sleep score:** {sleep_score}/100" if sleep_score is not None else "**Sleep:**"
        if total_sleep_min is not None:
            sleep_header += f" ({_fmt_duration_min((total_sleep_min or 0) * 60)} total sleep)"
        lines.append(sleep_header)
        if time_in_bed_min is not None:
            lines.append(f"- Time in bed: {_fmt_duration_min(time_in_bed_min * 60)}")
        if total_sleep_min is not None:
            eff_str = f" (efficiency {efficiency}%)" if efficiency is not None else ""
            lines.append(f"- Total sleep: {_fmt_duration_min(total_sleep_min * 60)}{eff_str}")
        stages = []
        if deep_sleep_min is not None:
            stages.append(f"deep {_fmt_duration_min(deep_sleep_min * 60)}")
        if rem_sleep_min is not None:
            stages.append(f"REM {_fmt_duration_min(rem_sleep_min * 60)}")
        if light_sleep_min is not None:
            stages.append(f"light {_fmt_duration_min(light_sleep_min * 60)}")
        if awake_min is not None:
            stages.append(f"awake {_fmt_duration_min(awake_min * 60)}")
        if stages:
            lines.append(f"- Stages: {', '.join(stages)}")
        hr_bits = []
        if avg_hrv is not None:
            hr_bits.append(f"avg HRV {avg_hrv} ms")
        if avg_hr is not None:
            hr_bits.append(f"avg HR {avg_hr:.0f}")
        if lowest_hr is not None:
            hr_bits.append(f"lowest HR {lowest_hr:.0f}")
        if avg_breath is not None:
            hr_bits.append(f"avg breath {avg_breath:.1f}/min")
        if hr_bits:
            lines.append(f"- {', '.join(hr_bits)}")
        if bedtime_start and bedtime_end:
            lines.append(
                f"- Bedtime: {bedtime_start.astimezone().strftime('%H:%M')} → "
                f"{bedtime_end.astimezone().strftime('%H:%M')}"
            )
        sleep_type = (sleep_session or {}).get("type")
        if sleep_type and sleep_type != "long_sleep":
            lines.append(f"- Type: {sleep_type}")
        lines.append("")

    if not (readiness or daily_sleep or sleep_session):
        lines.append("_No data for this day._")

    body = "\n".join(lines).rstrip() + "\n"

    captured_at = ""
    if readiness and readiness.get("timestamp"):
        captured_at = readiness["timestamp"]
    elif daily_sleep and daily_sleep.get("timestamp"):
        captured_at = daily_sleep["timestamp"]
    elif sleep_session and sleep_session.get("bedtime_start"):
        captured_at = sleep_session["bedtime_start"]

    derived = {
        "captured_at": captured_at,
        "day": day,
        "weekday": weekday,
        "readiness_score": readiness_score if readiness_score is not None else "",
        "sleep_score": sleep_score if sleep_score is not None else "",
        "total_sleep_min": total_sleep_min if total_sleep_min is not None else "",
        "deep_sleep_min": deep_sleep_min if deep_sleep_min is not None else "",
        "rem_sleep_min": rem_sleep_min if rem_sleep_min is not None else "",
        "hrv_avg": avg_hrv if avg_hrv is not None else "",
        "resting_hr": int(round(float(lowest_hr))) if lowest_hr is not None else "",
        "tags": [],
    }
    return body, derived


# ---------- main ingest ----------


def ingest_export(
    export_path: Path,
    kb_root: Path = DEFAULT_KB_ROOT,
    default_status: str = "confirmed",
) -> tuple[int, int]:
    payload = json.loads(export_path.read_text(encoding="utf-8"))

    workouts = (payload.get("workout") or {}).get("data") or []
    readiness_items = (payload.get("daily_readiness") or {}).get("data") or []
    daily_sleep_items = (payload.get("daily_sleep") or {}).get("data") or []
    sleep_items = (payload.get("sleep") or {}).get("data") or []

    ingested_at = _iso_utc_now()
    out_root = kb_root / "oura"
    out_root.mkdir(parents=True, exist_ok=True)
    conn = _ensure_db(kb_root / "index.db")
    written = 0
    skipped = 0

    try:
        # Workouts: one file per event.
        for w in workouts:
            wid = w.get("id")
            if not wid:
                skipped += 1
                continue
            body, derived = render_workout_markdown(w)
            md_path = out_root / f"workout-{wid}.md"
            title_bits = [(w.get("activity") or "workout").replace("_", " ").title()]
            if isinstance(derived["duration_min"], int):
                title_bits.append(f"— {derived['duration_min']} min")
            title = " ".join(title_bits)

            meta = {
                "id": f"oura:workout-{wid}",
                "source": "oura",
                "source_type": "workout",
                "title": title,
                "author": "",
                "url": "",
                "captured_at": derived["captured_at"],
                "ingested_at": ingested_at,
                "status": default_status,
                "day": derived["day"],
                "weekday": derived["weekday"],
                "activity": derived["activity"],
                "intensity": derived["intensity"],
                "duration_min": derived["duration_min"],
                "distance_m": derived["distance_m"],
                "calories": derived["calories"],
                "data_source": derived["data_source"],
                "tags": derived["tags"],
            }
            doc = _frontmatter(meta) + "\n\n" + body
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

        # Daily: union of days that have any of readiness/daily_sleep/sleep_session.
        readiness_by_day = _index_by_day(readiness_items)
        daily_sleep_by_day = _index_by_day(daily_sleep_items)
        main_sleep_by_day = _pick_main_sleep(sleep_items)

        all_days = sorted(
            set(readiness_by_day.keys())
            | set(daily_sleep_by_day.keys())
            | set(main_sleep_by_day.keys())
        )
        for day in all_days:
            body, derived = render_daily_markdown(
                day,
                readiness_by_day.get(day),
                daily_sleep_by_day.get(day),
                main_sleep_by_day.get(day),
            )
            md_path = out_root / f"daily-{day}.md"
            title = f"Oura — {day}" + (f" ({derived['weekday']})" if derived["weekday"] else "")

            meta = {
                "id": f"oura:daily-{day}",
                "source": "oura",
                "source_type": "daily",
                "title": title,
                "author": "",
                "url": "",
                "captured_at": derived["captured_at"],
                "ingested_at": ingested_at,
                "status": default_status,
                "day": derived["day"],
                "weekday": derived["weekday"],
                "readiness_score": derived["readiness_score"],
                "sleep_score": derived["sleep_score"],
                "total_sleep_min": derived["total_sleep_min"],
                "deep_sleep_min": derived["deep_sleep_min"],
                "rem_sleep_min": derived["rem_sleep_min"],
                "hrv_avg": derived["hrv_avg"],
                "resting_hr": derived["resting_hr"],
                "tags": derived["tags"],
            }
            doc = _frontmatter(meta) + "\n\n" + body
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("export", nargs="+", help="One or more raw Oura export JSON files.")
    p.add_argument("--kb-root", default=str(DEFAULT_KB_ROOT), help=f"Knowledge base root. Default: {DEFAULT_KB_ROOT}")
    p.add_argument("--status", default="confirmed", choices=["provisional", "confirmed"], help="Default status.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    kb_root = Path(args.kb_root).expanduser()
    total_written = 0
    total_skipped = 0
    for export in args.export:
        export_path = Path(export).expanduser()
        if not export_path.is_file():
            print(f"skip: {export_path} is not a file", file=sys.stderr)
            continue
        written, skipped = ingest_export(export_path, kb_root=kb_root, default_status=args.status)
        print(f"{export_path}: ingested={written} skipped={skipped}")
        total_written += written
        total_skipped += skipped
    print(f"Total: ingested={total_written} skipped={total_skipped} kb_root={kb_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
