#!/usr/bin/env python3
"""Google Calendar reads for the restaurant skills.

Reads the shared household calendar (calendars.shared) through gog. Read-only by design:
nothing here writes to the calendar.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import restaurant_common as rc  # noqa: E402

# An evening out is blocked by anything overlapping this window.
DINNER_START = dt.time(17, 30)
DINNER_END = dt.time(22, 0)

RESERVATION_RE = re.compile(
    r"^\s*(?:dinner|reservation|resy|reso)\s*(?:at|@|:)\s*(.+?)\s*$", re.I
)


def fetch_events(start: dt.date, end: dt.date) -> list[dict]:
    """Events between start and end (inclusive-ish), via gog."""
    cmd = [
        "gog", "calendar", "events",
        "-a", rc.GOG_ACCOUNT,
        "--calendars", rc.CALENDAR_ID,
        "--from", start.isoformat(),
        "--to", end.isoformat(),
        "--json", "--results-only",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"gog_failed: {r.stderr.strip()[:300]}")
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


def _parse(node: dict) -> tuple[dt.datetime | None, bool]:
    """Return (local datetime, is_all_day) for a start/end node."""
    if not node:
        return None, False
    if node.get("dateTime"):
        return dt.datetime.fromisoformat(node["dateTime"]), False
    if node.get("date"):
        return dt.datetime.fromisoformat(node["date"] + "T00:00:00"), True
    return None, False


def blockers_for(events: list[dict], day: dt.date) -> list[str]:
    """Summaries of everything that would keep the user from dinner out on `day`."""
    out: list[str] = []
    win_start = dt.datetime.combine(day, DINNER_START)
    win_end = dt.datetime.combine(day, DINNER_END)
    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        start, all_day = _parse(ev.get("start", {}))
        end, _ = _parse(ev.get("end", {}))
        if start is None:
            continue
        summary = (ev.get("summary") or "(untitled)").strip()
        if all_day:
            # All-day events are stored end-exclusive.
            end_day = (end or start).date()
            if start.date() <= day < end_day or start.date() == day:
                out.append(f"{summary} (all day)")
            continue
        if start.date() != day and (end is None or end.date() != day):
            continue
        s = start.replace(tzinfo=None)
        e = (end or start).replace(tzinfo=None)
        if e <= s:
            e = s + dt.timedelta(minutes=30)
        if s < win_end and e > win_start:
            out.append(f"{summary} ({s.strftime('%-I:%M%p').lower()})")
    return out


def existing_reservation(events: list[dict], day: dt.date) -> str | None:
    """Restaurant name if the day already has a reservation on the calendar."""
    found = reservation_detail(events, day)
    return found["name"] if found else None


def reservation_detail(events: list[dict], day: dt.date) -> dict | None:
    """Name AND start time of the day's reservation — the brief prints the time."""
    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        start, all_day = _parse(ev.get("start", {}))
        if start is None or start.date() != day:
            continue
        m = RESERVATION_RE.match(ev.get("summary") or "")
        if m:
            return {
                "name": m.group(1).strip(),
                "time": None if all_day else start.strftime("%-I:%M%p").lower(),
                "starts_at": start.isoformat(),
            }
    return None


def upcoming_thursdays(weeks: int = 4, from_date: dt.date | None = None) -> list[dt.date]:
    """The next `weeks` Thursdays, starting with the next one strictly ahead."""
    base = from_date or rc.today()
    ahead = (3 - base.weekday()) % 7 or 7  # Thursday == 3
    first = base + dt.timedelta(days=ahead)
    return [first + dt.timedelta(days=7 * i) for i in range(weeks)]


def free_thursdays(weeks: int = 4) -> list[dict]:
    days = upcoming_thursdays(weeks)
    events = fetch_events(days[0] - dt.timedelta(days=1), days[-1] + dt.timedelta(days=2))
    out = []
    for d in days:
        blockers = blockers_for(events, d)
        out.append({
            "date": d.isoformat(),
            "label": d.strftime("%a %b %-d"),
            "free": not blockers,
            "blockers": blockers,
            "reservation": existing_reservation(events, d),
        })
    return out
