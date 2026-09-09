#!/usr/bin/env python3
"""Build a compact context bundle for the workout coach agent.

SQL-backed (#2.1, 2026-05-04). Reads the structured metrics layer in
`knowledge/index.db` instead of walking markdown files:

  - sessions list   → `workouts` (Hevy only, mirroring V1 behavior)
  - per-exercise    → `exercise_sets` joined to `workouts`
  - recovery (Oura) → `metrics_daily_resolved` view + 30-day HRV baseline

Emits:
  1. Today's weekday + recommended slot (Mon=UB / Tue-Wed=LB canonical split).
  2. Recovery (Oura): readiness, sleep score, HRV (with % of 30-day baseline),
     RHR, total sleep min, plus a derived `recovery_band` (green/yellow/red)
     applying the SKILL.md recovery-gating table.
  3. Last 14 days of Hevy sessions (date, weekday, duration, exercise list).
  4. Per-exercise last working set in the last 365 days, with `days_since_last`
     to make the variety axis (rotate exercises within a pattern) trivial.

The agent reads GOALS.md + PROGRAM.md itself and cross-references this bundle
to propose today's session.

Usage:
    python3 build_context.py                         # uses today's date
    python3 build_context.py --date 2026-04-27       # simulate a given date
    python3 build_context.py --lookback-sessions 14  # tweak session window
    python3 build_context.py --lookback-perf 365     # tweak per-exercise window
    python3 build_context.py --json                  # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

KB_ROOT = Path.home() / ".openclaw" / "workspace" / "knowledge"

HRV_BASELINE_DAYS = 30
HRV_LOW_PCT = 0.70  # HRV < 70% of 30-day baseline is the "yellow" trigger per SKILL.md


def load_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        print(f"No KB index at {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def recommended_slot(weekday: str) -> tuple[str, str]:
    if weekday == "Monday":
        return ("Upper body (solo, logged in Hevy)",
                "Mon is the canonical UB slot.")
    if weekday in ("Tuesday", "Wednesday"):
        return ("Lower body (solo, logged in Hevy)",
                f"{weekday} is the canonical LB slot.")
    if weekday == "Friday":
        return ("Trainer day (full body, not logged in Hevy)",
                "Agent should not prescribe — trainer is driving.")
    if weekday == "Thursday":
        return ("Flex / make-up day",
                "Not part of canonical split. Pick whichever of UB/LB hasn't been hit in 4+ days.")
    return ("Rest / optional make-up",
            f"{weekday} is not a canonical training day. Only lift if UB or LB is 4+ days stale.")


def _resolved_metric(conn: sqlite3.Connection, metric: str, on_date: str) -> float | None:
    row = conn.execute(
        "SELECT value FROM metrics_daily_resolved WHERE metric=? AND date=?",
        (metric, on_date),
    ).fetchone()
    return row["value"] if row else None


def _hrv_baseline(conn: sqlite3.Connection, end_date: str, days: int = HRV_BASELINE_DAYS) -> float | None:
    row = conn.execute(
        """
        SELECT AVG(value) AS baseline
        FROM metrics_daily_resolved
        WHERE metric='heart_rate_variability'
          AND date BETWEEN date(?, ?) AND date(?, '-1 day')
        """,
        (end_date, f"-{days} days", end_date),
    ).fetchone()
    return row["baseline"] if row and row["baseline"] is not None else None


def _recovery_band(readiness: float | None, sleep: float | None, hrv_pct: float | None) -> str | None:
    """Apply the SKILL.md recovery-gating table.

    Returns 'green' / 'yellow' / 'red' / None (missing data — silent fall-through).
    """
    if readiness is None:
        return None
    band = "green" if readiness >= 80 else ("yellow" if readiness >= 65 else "red")
    # Secondary signals can escalate yellow only one step (per SKILL).
    if band == "green":
        if sleep is not None and sleep < 60:
            band = "yellow"
        elif hrv_pct is not None and hrv_pct < HRV_LOW_PCT:
            band = "yellow"
    return band


def load_recovery(conn: sqlite3.Connection, today: date) -> dict | None:
    """Load Oura/AH-resolved recovery for today, falling back to yesterday."""
    for offset in (0, 1):
        d = (today - _td(offset)).isoformat()
        readiness = _resolved_metric(conn, "readiness_score", d)
        if readiness is None:
            continue
        sleep = _resolved_metric(conn, "sleep_score", d)
        hrv = _resolved_metric(conn, "heart_rate_variability", d)
        rhr = _resolved_metric(conn, "resting_heart_rate", d)
        sleep_min = _resolved_metric(conn, "total_sleep_min", d)
        baseline = _hrv_baseline(conn, d)
        hrv_pct = (hrv / baseline) if (hrv is not None and baseline) else None
        return {
            "source_date": d,
            "age_days": offset,
            "readiness_score": _maybe_int(readiness),
            "sleep_score": _maybe_int(sleep),
            "hrv_avg": _maybe_int(hrv),
            "hrv_baseline_30d": round(baseline, 1) if baseline is not None else None,
            "hrv_pct_of_baseline": round(hrv_pct, 2) if hrv_pct is not None else None,
            "resting_hr": _maybe_int(rhr),
            "total_sleep_min": _maybe_int(sleep_min),
            "recovery_band": _recovery_band(readiness, sleep, hrv_pct),
        }
    return None


def load_sessions(conn: sqlite3.Connection, today: date, lookback_days: int) -> list[dict]:
    since = (today - _td(lookback_days)).isoformat()
    until = today.isoformat()
    workout_rows = conn.execute(
        """
        SELECT id, date, started_at, duration_min, title
        FROM workouts
        WHERE source='hevy' AND date BETWEEN ? AND ?
        ORDER BY started_at DESC
        """,
        (since, until),
    ).fetchall()
    sessions = []
    for w in workout_rows:
        ex_rows = conn.execute(
            """
            SELECT exercise_title
            FROM exercise_sets
            WHERE workout_id = ?
            GROUP BY exercise_title
            ORDER BY MIN(set_index)
            """,
            (w["id"],),
        ).fetchall()
        sessions.append({
            "date": w["date"],
            "weekday": _weekday_from_iso(w["started_at"]),
            "duration_min": int(w["duration_min"]) if w["duration_min"] is not None else None,
            "exercises": [r["exercise_title"] for r in ex_rows],
        })
    return sessions


def load_last_performance(conn: sqlite3.Connection, today: date, lookback_days: int) -> list[dict]:
    """Per-exercise: latest workout containing it, with that workout's top working set."""
    since = (today - _td(lookback_days)).isoformat()
    until = today.isoformat()
    rows = conn.execute(
        """
        SELECT s.exercise_title, s.weight_lb, s.reps, s.set_index,
               w.id AS workout_id, w.date, w.started_at
        FROM exercise_sets s
        JOIN workouts w ON w.id = s.workout_id
        WHERE w.source='hevy' AND w.date BETWEEN ? AND ? AND s.is_warmup = 0
        ORDER BY s.exercise_title, w.started_at DESC, s.set_index ASC
        """,
        (since, until),
    ).fetchall()

    # Group by exercise; keep only sets from each exercise's latest workout.
    grouped: dict[str, dict] = {}
    for r in rows:
        ex = r["exercise_title"]
        if ex not in grouped:
            grouped[ex] = {
                "_workout_id": r["workout_id"],
                "_started_at": r["started_at"],
                "date": r["date"],
                "_sets": [(r["weight_lb"], r["reps"])],
            }
        elif grouped[ex]["_workout_id"] == r["workout_id"]:
            grouped[ex]["_sets"].append((r["weight_lb"], r["reps"]))
        # else: ignore older workouts for this exercise

    out = []
    for ex, info in grouped.items():
        sets = info["_sets"]
        # Sets with reps populated are usable. Timed-only sets (Plank etc., reps NULL)
        # are skipped — no meaningful "top working set" to report.
        rep_sets = [(w if w is not None else 0.0, r) for w, r in sets if r is not None]
        if not rep_sets:
            continue
        max_w = max(w for w, _ in rep_sets)
        top_reps = max(r for w, r in rep_sets if w == max_w)
        last_d = date.fromisoformat(info["date"])
        out.append({
            "exercise": ex,
            "last_date": info["date"],
            "last_weekday": _weekday_from_iso(info["_started_at"]),
            "days_since_last": (today - last_d).days,
            "top_weight_lb": max_w,
            "top_reps": top_reps,
            "bodyweight": max_w == 0,
            "working_sets": len(rep_sets),
        })
    out.sort(key=lambda e: e["exercise"].lower())
    return out


def build_context(today: date, db_path: Path, lookback_sessions: int, lookback_perf: int) -> dict:
    conn = load_db(db_path)
    weekday = today.strftime("%A")
    slot, slot_reason = recommended_slot(weekday)
    return {
        "today": today.isoformat(),
        "weekday": weekday,
        "recommended_slot": slot,
        "slot_rationale": slot_reason,
        "lookback_sessions_days": lookback_sessions,
        "lookback_perf_days": lookback_perf,
        "sessions": load_sessions(conn, today, lookback_sessions),
        "last_performance": load_last_performance(conn, today, lookback_perf),
        "readiness": load_recovery(conn, today),
    }


def render_text(ctx: dict) -> str:
    out: list[str] = []
    out.append("=== WORKOUT COACH CONTEXT ===")
    out.append(f"Today: {ctx['weekday']} {ctx['today']}")
    out.append(f"Recommended slot: {ctx['recommended_slot']}")
    out.append(f"  ({ctx['slot_rationale']})")
    out.append("")

    r = ctx.get("readiness")
    out.append("=== RECOVERY (Oura) ===")
    if not r:
        out.append("  (no Oura data for today or yesterday — fall through silently)")
    else:
        age_label = "today" if r["age_days"] == 0 else f"yesterday ({r['source_date']})"
        parts = [f"Source: {age_label}", f"readiness {r['readiness_score']}"]
        if r.get("sleep_score") is not None:
            parts.append(f"sleep {r['sleep_score']}")
        if r.get("hrv_avg") is not None:
            hrv_part = f"HRV {r['hrv_avg']} ms"
            if r.get("hrv_baseline_30d") is not None:
                hrv_part += f" (30d base {r['hrv_baseline_30d']}, "
                hrv_part += f"{int(r['hrv_pct_of_baseline'] * 100)}% of base)"
            parts.append(hrv_part)
        if r.get("resting_hr") is not None:
            parts.append(f"RHR {r['resting_hr']}")
        if r.get("total_sleep_min") is not None:
            parts.append(f"sleep {r['total_sleep_min']} min")
        if r.get("recovery_band"):
            parts.append(f"band={r['recovery_band']}")
        out.append("  " + " | ".join(parts))
    out.append("")

    out.append(f"=== LAST {ctx['lookback_sessions_days']} DAYS (newest first) ===")
    if not ctx["sessions"]:
        out.append("  (no sessions logged in window)")
    else:
        for s in ctx["sessions"]:
            dur = f"{s['duration_min']}min" if s["duration_min"] else "?min"
            exlist = " / ".join(s["exercises"]) if s["exercises"] else "(empty workout)"
            out.append(f"  {s['date']} {s['weekday'][:3]} — {dur} — {exlist}")
    out.append("")

    out.append(f"=== EXERCISE LAST WORKING SET ({ctx['lookback_perf_days']} day window) ===")
    if not ctx["last_performance"]:
        out.append("  (no exercises logged in window)")
    else:
        for e in ctx["last_performance"]:
            if e["bodyweight"]:
                load = f"BW × {e['top_reps']}"
            else:
                load = f"{_fmt_w(e['top_weight_lb'])} lb × {e['top_reps']}"
            out.append(
                f"  {e['exercise']} — {e['last_date']} ({e['last_weekday'][:3]}, "
                f"{e['days_since_last']}d ago) — top set: {load}  "
                f"[{e['working_sets']} working sets]"
            )
    out.append("")
    out.append("=== END CONTEXT ===")
    return "\n".join(out)


def _td(days: int):
    from datetime import timedelta
    return timedelta(days=days)


def _weekday_from_iso(ts: str) -> str:
    """Parse a started_at string (Hevy's ISO8601 with TZ) and return weekday name."""
    s = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).strftime("%A")
    except ValueError:
        return date.fromisoformat(s[:10]).strftime("%A")


def _maybe_int(x: float | None) -> int | float | None:
    if x is None:
        return None
    return int(x) if x == int(x) else round(x, 1)


def _fmt_w(x: float) -> str:
    return f"{int(x)}" if x == int(x) else f"{x:g}"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", help="Override today's date (YYYY-MM-DD). Default: actual today in local time.")
    p.add_argument("--kb-root", default=str(KB_ROOT))
    p.add_argument("--lookback-sessions", type=int, default=14, help="Days of recent sessions to list.")
    p.add_argument("--lookback-perf", type=int, default=365, help="Days for per-exercise last-performance window. Coach applies its own staleness rules against last_date.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    today = date.fromisoformat(args.date) if args.date else date.today()
    kb_root = Path(args.kb_root).expanduser()
    db_path = kb_root / "index.db"
    ctx = build_context(today, db_path, args.lookback_sessions, args.lookback_perf)
    if args.json:
        print(json.dumps(ctx, indent=2, ensure_ascii=False))
    else:
        print(render_text(ctx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
