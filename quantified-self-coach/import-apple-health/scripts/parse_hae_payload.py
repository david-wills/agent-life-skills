"""Parse a Health Auto Export JSON payload into a per-day Markdown record.

HAE writes (and POSTs) two payload shapes:

  Daily metrics: {"data": {"metrics": [{name, units, data: [{date, qty/Avg/Min/Max, source}]}]}}
  Workouts:     {"data": {"workouts": [{name, start, end, heartRate, ...}]}}

Both shapes are merged into one file per day at:
    knowledge/apple-health/YYYY-MM-DD.md

The parser is idempotent: passing the same payload twice yields the same
markdown. When metrics for a date already exist, the new payload's values win
(latest sync is authoritative).

Tweakable knobs at the top of this module:
  CORE_METRICS  — names that surface in the "Core" section.
  CORE_LABELS   — display labels for core metrics.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Ordered by surfacing priority (Tier 1 → Tier 4). For metrics that overlap
# with Oura (resting HR, HRV, respiratory rate), Oura is the primary source
# downstream; Apple is kept here as confirmation / fallback when Oura is missing.
# See SKILL.md "Surfacing priority tiers" for the full policy.
CORE_METRICS = [
    # Tier 1 — primary signals
    "vo2_max",
    "resting_heart_rate",
    "heart_rate_variability",
    "respiratory_rate",
    "apple_exercise_time",
    "active_energy",
    # Tier 2 — useful daily
    "step_count",
    "time_in_daylight",
    "weight_body_mass",
    "body_fat_percentage",
    "mindful_minutes",
    # Tier 3 — passive monitoring
    "apple_stand_hour",
    "apple_stand_time",
    # Tier 4 — lowest priority
    "environmental_audio_exposure",
    "headphone_audio_exposure",
]

CORE_LABELS = {
    "vo2_max": "VO2 max",
    "resting_heart_rate": "Resting HR",
    "heart_rate_variability": "HRV",
    "respiratory_rate": "Respiratory rate",
    "apple_exercise_time": "Exercise minutes",
    "active_energy": "Active energy",
    "step_count": "Steps",
    "time_in_daylight": "Time in daylight",
    "weight_body_mass": "Body weight",
    "body_fat_percentage": "Body fat %",
    "mindful_minutes": "Mindful minutes",
    "apple_stand_hour": "Stand hours",
    "apple_stand_time": "Stand minutes",
    "environmental_audio_exposure": "Environmental audio exposure",
    "headphone_audio_exposure": "Headphone audio exposure",
}

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _date_from_str(s: str) -> str | None:
    if not isinstance(s, str):
        return None
    m = DATE_RE.match(s)
    return m.group(1) if m else None


def _fmt_qty(qty: Any, units: str | None) -> str:
    if qty is None:
        return "—"
    if isinstance(qty, (int, float)):
        if isinstance(qty, float):
            qty_str = f"{qty:,.2f}".rstrip("0").rstrip(".")
        else:
            qty_str = f"{qty:,}"
    else:
        qty_str = str(qty)
    if units in (None, "", "count"):
        return qty_str
    return f"{qty_str} {units}"


@dataclass
class DayBundle:
    """All HAE data for a single calendar date."""

    date: str
    metrics: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    workouts: list[dict[str, Any]] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)

    def add_metric(self, name: str, units: str, point: dict[str, Any]) -> None:
        entry = {"units": units, **point}
        self.metrics.setdefault(name, []).append(entry)

    def add_workout(self, w: dict[str, Any]) -> None:
        self.workouts.append(w)


def split_payload_by_day(payload: dict[str, Any]) -> dict[str, DayBundle]:
    """Group all metric points + workouts in `payload` by calendar date.

    Accepts either of HAE's payload shapes (or both merged into one dict).
    """
    by_day: dict[str, DayBundle] = {}
    data = payload.get("data") or {}

    for metric in data.get("metrics") or []:
        name = metric.get("name")
        units = metric.get("units")
        if not name:
            continue
        for point in metric.get("data") or []:
            date = _date_from_str(point.get("date") or "")
            if not date:
                continue
            bundle = by_day.setdefault(date, DayBundle(date=date))
            bundle.add_metric(name, units, point)
            src = point.get("source")
            if src:
                bundle.sources.add(src)

    for workout in data.get("workouts") or []:
        date = _date_from_str(workout.get("start") or "")
        if not date:
            continue
        bundle = by_day.setdefault(date, DayBundle(date=date))
        bundle.add_workout(workout)

    return by_day


def render_day_markdown(bundle: DayBundle) -> str:
    lines: list[str] = []
    metric_names = sorted(bundle.metrics.keys())
    sources = sorted(bundle.sources)

    lines.append("---")
    lines.append(f"date: {bundle.date}")
    lines.append("source: apple-health")
    lines.append(f"metric_count: {len(bundle.metrics)}")
    lines.append(f"workout_count: {len(bundle.workouts)}")
    if sources:
        lines.append("device_sources:")
        for s in sources:
            lines.append(f"  - {s!r}")
    lines.append(f"updated_at: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Apple Health — {bundle.date}")
    lines.append("")

    # Core metrics section
    core_lines: list[str] = []
    for name in CORE_METRICS:
        if name not in bundle.metrics:
            continue
        label = CORE_LABELS.get(name, name)
        core_lines.append(f"- **{label}**: {_summarize_metric_points(bundle.metrics[name])}")
    if core_lines:
        lines.append("## Core")
        lines.append("")
        lines.extend(core_lines)
        lines.append("")

    # Workouts section
    if bundle.workouts:
        lines.append("## Workouts")
        lines.append("")
        for w in bundle.workouts:
            lines.extend(_render_workout(w))
        lines.append("")

    # Other metrics: everything not in CORE_METRICS, alphabetised.
    other = [n for n in metric_names if n not in CORE_METRICS]
    if other:
        lines.append("## Other metrics")
        lines.append("")
        for name in other:
            lines.append(f"- **{name}**: {_summarize_metric_points(bundle.metrics[name])}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _summarize_metric_points(points: list[dict[str, Any]]) -> str:
    """Summarise a metric's points for one day. Handles both qty and Avg/Min/Max shapes."""
    if not points:
        return "—"
    units = next((p.get("units") for p in points if p.get("units")), None)
    # Aggregated daily metrics typically have one point per day.
    if len(points) == 1:
        p = points[0]
        if "qty" in p:
            return _fmt_qty(p.get("qty"), units)
        if "Avg" in p or "Min" in p or "Max" in p:
            avg = p.get("Avg")
            mn = p.get("Min")
            mx = p.get("Max")
            return f"avg {_fmt_qty(avg, units)} (min {_fmt_qty(mn, units)} / max {_fmt_qty(mx, units)})"
        # Sleep_analysis-like — drop the whole dict (compact).
        return json.dumps({k: v for k, v in p.items() if k not in ("source",)}, ensure_ascii=False)
    # Multiple points: total qty if numeric, else range.
    qtys = [p.get("qty") for p in points if isinstance(p.get("qty"), (int, float))]
    if qtys:
        total = sum(qtys)
        return f"{_fmt_qty(total, units)} (sum of {len(qtys)} entries)"
    return f"{len(points)} entries"


def _render_workout(w: dict[str, Any]) -> list[str]:
    name = w.get("name") or "Workout"
    start = w.get("start") or ""
    end = w.get("end") or ""
    duration_s = w.get("duration") or 0
    minutes = round(duration_s / 60) if isinstance(duration_s, (int, float)) else 0

    hr = w.get("heartRate") or {}
    hr_min = (hr.get("min") or {}).get("qty")
    hr_max = (hr.get("max") or {}).get("qty")
    hr_avg = (hr.get("avg") or {}).get("qty")

    energy = (w.get("activeEnergyBurned") or {}).get("qty")

    out = [f"### {name}"]
    if start:
        out.append(f"- **Time**: {start}{' → ' + end[11:19] if end else ''}")
    if minutes:
        out.append(f"- **Duration**: {minutes} min")
    if hr_avg is not None or hr_min is not None or hr_max is not None:
        bits: list[str] = []
        if hr_avg is not None:
            bits.append(f"avg {round(hr_avg)}")
        if hr_min is not None:
            bits.append(f"min {round(hr_min)}")
        if hr_max is not None:
            bits.append(f"max {round(hr_max)}")
        out.append(f"- **Heart rate**: {' / '.join(bits)} bpm")
    if energy is not None:
        out.append(f"- **Active energy**: {round(energy)} kcal")
    intensity = (w.get("intensity") or {}).get("qty")
    if intensity is not None:
        out.append(f"- **Intensity**: {intensity}")
    step_count = w.get("stepCount")
    if isinstance(step_count, list) and step_count:
        steps = sum(p.get("qty", 0) or 0 for p in step_count)
        out.append(f"- **Steps**: {round(steps):,}")
    out.append("")
    return out


def merge_into_existing(
    out_dir: Path, by_day: dict[str, DayBundle], raw_archive_dir: Path | None = None
) -> list[Path]:
    """Write each DayBundle to its per-day markdown, merging with any prior state.

    The "merge" rule: when the new payload contains a date that already has a
    file, parse the prior file's metrics from frontmatter (we trust ourselves)
    and merge — but for any metric *name* present in the new payload, the new
    points fully replace the old ones for that name. Workouts are merged by
    workout `id` (new ones added, existing ones replaced).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for date, bundle in by_day.items():
        existing = _load_existing_bundle(out_dir, date)
        if existing:
            for name, pts in existing.metrics.items():
                bundle.metrics.setdefault(name, pts)
            existing_ids = {w.get("id") for w in bundle.workouts if w.get("id")}
            for w in existing.workouts:
                if w.get("id") and w["id"] in existing_ids:
                    continue
                bundle.workouts.append(w)
            bundle.sources.update(existing.sources)
        path = out_dir / f"{date}.md"
        path.write_text(render_day_markdown(bundle), encoding="utf-8")
        written.append(path)
    return written


def _load_existing_bundle(out_dir: Path, date: str) -> DayBundle | None:
    path = out_dir / f"{date}.md"
    if not path.exists():
        return None
    sidecar = out_dir / ".sidecar" / f"{date}.json"
    if not sidecar.exists():
        return None
    try:
        d = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    bundle = DayBundle(date=date)
    bundle.metrics = d.get("metrics", {})
    bundle.workouts = d.get("workouts", [])
    bundle.sources = set(d.get("sources", []))
    return bundle


def write_sidecar(out_dir: Path, by_day: dict[str, DayBundle]) -> None:
    sidecar_dir = out_dir / ".sidecar"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    for date, bundle in by_day.items():
        existing = _load_existing_bundle(out_dir, date)
        merged_metrics = dict(existing.metrics) if existing else {}
        merged_metrics.update(bundle.metrics)
        merged_workouts: dict[str, dict[str, Any]] = {}
        if existing:
            for w in existing.workouts:
                key = w.get("id") or f"{w.get('start')}-{w.get('name')}"
                merged_workouts[key] = w
        for w in bundle.workouts:
            key = w.get("id") or f"{w.get('start')}-{w.get('name')}"
            merged_workouts[key] = w
        merged_sources = (existing.sources if existing else set()) | bundle.sources
        sidecar_path = sidecar_dir / f"{date}.json"
        sidecar_path.write_text(
            json.dumps(
                {
                    "date": date,
                    "metrics": merged_metrics,
                    "workouts": list(merged_workouts.values()),
                    "sources": sorted(merged_sources),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def archive_raw_payload(payload: dict[str, Any], archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    out = archive_dir / f"hae-{ts}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return out


def ingest_payload(
    payload: dict[str, Any],
    *,
    out_dir: Path,
    raw_archive_dir: Path | None,
) -> dict[str, Any]:
    """Parse + persist + return a small report describing what changed."""
    by_day = split_payload_by_day(payload)
    raw_path: Path | None = None
    if raw_archive_dir is not None:
        raw_path = archive_raw_payload(payload, raw_archive_dir)
    write_sidecar(out_dir, by_day)
    by_day_with_existing = {date: _load_existing_bundle(out_dir, date) or bundle for date, bundle in by_day.items()}
    written = merge_into_existing(out_dir, by_day_with_existing)
    return {
        "dates": sorted(by_day.keys()),
        "files_written": [str(p) for p in written],
        "raw_archive": str(raw_path) if raw_path else None,
        "metric_points": sum(sum(len(v) for v in b.metrics.values()) for b in by_day.values()),
        "workout_count": sum(len(b.workouts) for b in by_day.values()),
    }


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Parse one HAE JSON file → per-day markdown")
    p.add_argument("input", type=Path, help="Path to HAE JSON file")
    p.add_argument("--out-dir", type=Path, default=Path("knowledge/apple-health"))
    p.add_argument("--raw-archive", type=Path, default=Path("imports/apple-health/raw"))
    args = p.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report = ingest_payload(payload, out_dir=args.out_dir, raw_archive_dir=args.raw_archive)
    print(json.dumps(report, indent=2))
