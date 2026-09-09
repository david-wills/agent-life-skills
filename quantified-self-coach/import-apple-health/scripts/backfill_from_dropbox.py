"""One-time historical import of HAE JSON files from Dropbox.

Reads:
  ~/Dropbox/Apps/Health Auto Export/Health Auto Export/Apple_Health/*.json
  ~/Dropbox/Apps/Health Auto Export/Health Auto Export/Apple_Health_Workouts/*.json

Writes:
  knowledge/apple-health/YYYY-MM-DD.md  (per-day, merged)

Each file is processed via parse_hae_payload.ingest_payload, so behavior is
identical to the live REST server. Run this once; rerunning is idempotent.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from parse_hae_payload import ingest_payload  # noqa: E402

DEFAULT_DROPBOX_BASE = Path.home() / "Dropbox" / "Apps" / "Health Auto Export" / "Health Auto Export"


def _iter_payload_files(base: Path) -> list[Path]:
    paths: list[Path] = []
    for sub in ("Apple_Health", "Apple_Health_Workouts"):
        d = base / sub
        if not d.is_dir():
            continue
        paths.extend(sorted(d.glob("HealthAutoExport-*.json")))
    return paths


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill HAE history from Dropbox")
    p.add_argument(
        "--dropbox-base",
        type=Path,
        default=DEFAULT_DROPBOX_BASE,
        help="Path containing Apple_Health/ and Apple_Health_Workouts/",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("knowledge/apple-health"),
    )
    p.add_argument(
        "--raw-archive",
        type=Path,
        default=None,
        help="If set, archive each payload here; default: skip raw archiving (we already have the originals in Dropbox)",
    )
    p.add_argument("--limit", type=int, default=0, help="Process at most N files (0 = all)")
    p.add_argument("--progress-every", type=int, default=50)
    args = p.parse_args()

    files = _iter_payload_files(args.dropbox_base)
    if args.limit:
        files = files[: args.limit]
    print(f"found {len(files)} HAE files under {args.dropbox_base}", flush=True)
    if not files:
        return 0

    started = time.time()
    total_dates: set[str] = set()
    total_metric_points = 0
    total_workouts = 0
    failures: list[tuple[str, str]] = []

    for i, fp in enumerate(files, 1):
        try:
            payload = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append((str(fp), f"read: {exc}"))
            continue
        try:
            report = ingest_payload(payload, out_dir=args.out_dir, raw_archive_dir=args.raw_archive)
        except Exception as exc:
            failures.append((str(fp), f"ingest: {exc}"))
            continue
        total_dates.update(report.get("dates", []))
        total_metric_points += report.get("metric_points", 0)
        total_workouts += report.get("workout_count", 0)
        if i % args.progress_every == 0 or i == len(files):
            elapsed = time.time() - started
            rate = i / elapsed if elapsed else 0.0
            print(f"[{i}/{len(files)}] {fp.name}  rate={rate:.1f} files/s", flush=True)

    elapsed = time.time() - started
    print(
        json.dumps(
            {
                "files_processed": len(files) - len(failures),
                "files_failed": len(failures),
                "unique_dates": len(total_dates),
                "metric_points": total_metric_points,
                "workouts": total_workouts,
                "elapsed_seconds": round(elapsed, 1),
                "failures_sample": failures[:5],
            },
            indent=2,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
