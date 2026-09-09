"""Parse a Health Auto Export JSON payload into a per-day Markdown record.

HAE writes (and POSTs) two payload shapes:

  Daily metrics: {"data": {"metrics": [{name, units, data: [{date, qty/Avg/Min/Max, source}]}]}}
  Workouts:     {"data": {"workouts": [{name, start, end, heartRate, ...}]}}

Both shapes are merged into one file per day at:
    <data_root>/knowledge/apple-health/YYYY-MM-DD.md

with a JSON sidecar holding the unrendered points at:
    <data_root>/knowledge/apple-health/.sidecar/YYYY-MM-DD.json

The sidecar is the record; the markdown is a rendering of it. Merging is
idempotent: passing the same payload twice yields the same files. When a date
already has data, any metric *name* present in the new payload replaces the old
points for that name (latest sync is authoritative); workouts are keyed by id
(or start+name) and replaced individually; device sources are unioned.

If a per-day `.md` exists without a sidecar, the merge recovers what it can from
the markdown (one point per metric line, the headline numbers per workout) so a
later sync never silently discards the day. The recovered state is written to
the sidecar on that merge, and the sidecar is authoritative from then on.

Tweakable knobs at the top of this module:
  CORE_METRICS  — names that surface in the "Core" section.
  CORE_LABELS   — display labels for core metrics.

CLI:
  python3 parse_hae_payload.py export.json                # merge one file
  python3 parse_hae_payload.py export.json --no-archive   # do not copy the raw JSON
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

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
_LABEL_TO_NAME = {v: k for k, v in CORE_LABELS.items()}

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# One writer at a time per process. The server is threaded and two HAE POSTs
# (metrics + workouts) for the same day can land in the same second; without
# this the second read-modify-write clobbers the first.
_MERGE_LOCK = threading.Lock()


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


# ---------- recovering prior state ------------------------------------------

_NUM_RE = r"-?[\d,]+(?:\.\d+)?"
_METRIC_LINE_RE = re.compile(r"^- \*\*(?P<label>[^*]+)\*\*: (?P<value>.+)$")
_QTY_RE = re.compile(rf"^(?P<qty>{_NUM_RE})(?: (?P<units>[^(]+?))?(?: \(sum of \d+ entries\))?$")
_AVG_RE = re.compile(
    rf"^avg (?P<avg>{_NUM_RE}|—)(?: (?P<units>[^(]+?))? \(min (?P<min>{_NUM_RE}|—)(?: [^/]+?)? / max (?P<max>{_NUM_RE}|—)(?: [^)]+?)?\)$"
)


def _num(s: str | None) -> float | None:
    if s is None or s == "—":
        return None
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return None
    return int(v) if v == int(v) else v


def _point_from_summary(value: str) -> dict[str, Any] | None:
    """Invert _summarize_metric_points for the shapes it emits. Lossy by design."""
    m = _QTY_RE.match(value)
    if m:
        qty = _num(m.group("qty"))
        if qty is None:
            return None
        return {"units": (m.group("units") or "").strip() or None, "qty": qty}
    m = _AVG_RE.match(value)
    if m:
        return {
            "units": (m.group("units") or "").strip() or None,
            "Avg": _num(m.group("avg")),
            "Min": _num(m.group("min")),
            "Max": _num(m.group("max")),
        }
    if value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _bundle_from_markdown(path: Path, date: str) -> DayBundle:
    """Best-effort recovery of a day's bundle from its rendered markdown.

    Only used when the sidecar is missing. Metrics come back as one point each;
    workouts keep name, start, duration, heart rate and energy. Anything the
    renderer summarised away (per-entry points, step lists) is gone.
    """
    bundle = DayBundle(date=date)
    text = path.read_text(encoding="utf-8")
    fm_match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    fm_text, body = (fm_match.group(1), fm_match.group(2)) if fm_match else ("", text)
    for line in fm_text.splitlines():
        line = line.strip()
        if line.startswith("- '") and line.endswith("'"):
            bundle.sources.add(line[3:-1])

    section = ""
    workout: dict[str, Any] | None = None
    for line in body.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            workout = None
            continue
        if section == "Workouts":
            if line.startswith("### "):
                workout = {"name": line[4:].strip()}
                bundle.workouts.append(workout)
                continue
            m = _METRIC_LINE_RE.match(line)
            if not (m and workout is not None):
                continue
            label, value = m.group("label"), m.group("value")
            if label == "Time":
                workout["start"] = value.split(" → ")[0].strip()
            elif label == "Duration":
                mins = _num(value.split(" ")[0])
                if mins is not None:
                    workout["duration"] = mins * 60
            elif label == "Heart rate":
                hr: dict[str, Any] = {}
                for bit in value.replace(" bpm", "").split(" / "):
                    k, _, v = bit.partition(" ")
                    if k in ("avg", "min", "max") and _num(v) is not None:
                        hr[k] = {"qty": _num(v)}
                if hr:
                    workout["heartRate"] = hr
            elif label == "Active energy":
                kcal = _num(value.split(" ")[0])
                if kcal is not None:
                    workout["activeEnergyBurned"] = {"qty": kcal}
            continue
        if section in ("Core", "Other metrics"):
            m = _METRIC_LINE_RE.match(line)
            if not m:
                continue
            label, value = m.group("label"), m.group("value")
            name = _LABEL_TO_NAME.get(label, label)
            point = _point_from_summary(value.strip())
            if point is not None:
                bundle.metrics[name] = [point]
    return bundle


def _load_existing_bundle(out_dir: Path, date: str) -> DayBundle | None:
    """Prior state for a date: the sidecar if present, else recovered from the markdown."""
    md_path = out_dir / f"{date}.md"
    sidecar = out_dir / ".sidecar" / f"{date}.json"
    if sidecar.exists():
        try:
            d = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = None
        if d is not None:
            bundle = DayBundle(date=date)
            bundle.metrics = d.get("metrics", {})
            bundle.workouts = d.get("workouts", [])
            bundle.sources = set(d.get("sources", []))
            return bundle
    if md_path.exists():
        try:
            return _bundle_from_markdown(md_path, date)
        except OSError:
            return None
    return None


# ---------- merging ------------------------------------------------------------

def _workout_key(w: dict[str, Any]) -> str:
    return w.get("id") or f"{w.get('start')}-{w.get('name')}"


def merge_bundles(existing: DayBundle | None, new: DayBundle) -> DayBundle:
    """Apply the merge rule described in the module docstring. Pure."""
    merged = DayBundle(date=new.date)
    merged.metrics = dict(existing.metrics) if existing else {}
    merged.metrics.update(new.metrics)
    workouts: dict[str, dict[str, Any]] = {}
    for w in (existing.workouts if existing else []):
        workouts[_workout_key(w)] = w
    for w in new.workouts:
        workouts[_workout_key(w)] = w
    merged.workouts = list(workouts.values())
    merged.sources = (existing.sources if existing else set()) | new.sources
    return merged


def _write_day(out_dir: Path, bundle: DayBundle) -> Path:
    sidecar_dir = out_dir / ".sidecar"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / f"{bundle.date}.json").write_text(
        json.dumps(
            {
                "date": bundle.date,
                "metrics": bundle.metrics,
                "workouts": bundle.workouts,
                "sources": sorted(bundle.sources),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    path = out_dir / f"{bundle.date}.md"
    path.write_text(render_day_markdown(bundle), encoding="utf-8")
    return path


def merge_into_existing(out_dir: Path, by_day: dict[str, DayBundle]) -> list[Path]:
    """Merge each DayBundle into its per-day sidecar + markdown. Serialised per process."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for date, bundle in sorted(by_day.items()):
        with _MERGE_LOCK:
            merged = merge_bundles(_load_existing_bundle(out_dir, date), bundle)
            written.append(_write_day(out_dir, merged))
    return written


def archive_raw_payload(payload: dict[str, Any], archive_dir: Path) -> Path:
    """Copy the raw payload aside. Microsecond stamp plus a counter, so two POSTs
    landing in the same second never overwrite each other."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f")
    out = archive_dir / f"hae-{ts}.json"
    n = 1
    while out.exists():
        out = archive_dir / f"hae-{ts}-{n}.json"
        n += 1
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
    written = merge_into_existing(out_dir, by_day)
    return {
        "dates": sorted(by_day.keys()),
        "files_written": [str(p) for p in written],
        "raw_archive": str(raw_path) if raw_path else None,
        "metric_points": sum(sum(len(v) for v in b.metrics.values()) for b in by_day.values()),
        "workout_count": sum(len(b.workouts) for b in by_day.values()),
    }


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Merge one HAE JSON file into the per-day markdown + sidecar.")
    p.add_argument("input", type=Path, help="Path to an HAE JSON export.")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="Per-day markdown directory. Default: <data_root>/knowledge/apple-health")
    p.add_argument("--raw-archive", type=Path, default=None,
                   help="Where to copy the raw payload. Default: <data_root>/imports/apple-health/raw")
    p.add_argument("--no-archive", action="store_true", help="Do not copy the raw payload anywhere.")
    args = p.parse_args()

    from skill_config import data_root

    out_dir = args.out_dir or data_root() / "knowledge" / "apple-health"
    raw_dir = None if args.no_archive else (args.raw_archive or data_root() / "imports" / "apple-health" / "raw")
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("input is not a JSON object", file=sys.stderr)
        return 2
    report = ingest_payload(payload, out_dir=out_dir, raw_archive_dir=raw_dir)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
