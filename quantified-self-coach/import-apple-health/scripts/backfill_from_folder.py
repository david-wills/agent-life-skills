"""Re-play archived HAE JSON exports from any folder.

Walks FOLDER recursively for ``*.json``, feeds each through
``parse_hae_payload.ingest_payload`` — exactly what the live server does — and
merges the result into <data_root>/knowledge/apple-health/. Use it for a
historical import from wherever the Health Auto Export app used to drop files
(a cloud-sync folder, a laptop backup), or to rebuild the per-day files from
the server's own raw archive. Idempotent; rerunning is harmless.

Usage:
  python3 backfill_from_folder.py ~/some/HAE-exports
  python3 backfill_from_folder.py <data_root>/imports/apple-health/raw --dry-run
"""

from __future__ import annotations

import argparse
import os
import json
import sys
import time
from pathlib import Path

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from parse_hae_payload import ingest_payload, split_payload_by_day  # noqa: E402


def _iter_payload_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.json") if p.is_file() and not p.name.startswith("."))


def main() -> int:
    os.umask(0o077)  # health data: every file this run creates is owner-only
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", type=Path, help="Folder to walk for HAE JSON exports (recursive).")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="Per-day markdown directory. Default: <data_root>/knowledge/apple-health")
    p.add_argument("--raw-archive", type=Path, default=None,
                   help="If set, copy each payload here too. Default: no copy (the originals are the archive).")
    p.add_argument("--limit", type=int, default=0, help="Process at most N files (0 = all).")
    p.add_argument("--progress-every", type=int, default=50, help="Print a progress line every N files.")
    p.add_argument("--dry-run", action="store_true", help="Parse and count; write nothing.")
    args = p.parse_args()

    folder = args.folder.expanduser()
    if not folder.is_dir():
        print(f"not a directory: {folder}", file=sys.stderr)
        return 2
    if args.out_dir is not None:
        out_dir = args.out_dir
    else:
        from skill_config import data_root
        out_dir = data_root() / "knowledge" / "apple-health"

    files = _iter_payload_files(folder)
    if args.limit:
        files = files[: args.limit]
    print(f"found {len(files)} JSON files under {folder}", flush=True)
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
        if not isinstance(payload, dict):
            failures.append((str(fp), "not a JSON object"))
            continue
        try:
            if args.dry_run:
                by_day = split_payload_by_day(payload)
                report = {
                    "dates": sorted(by_day),
                    "metric_points": sum(sum(len(v) for v in b.metrics.values()) for b in by_day.values()),
                    "workout_count": sum(len(b.workouts) for b in by_day.values()),
                }
            else:
                report = ingest_payload(payload, out_dir=out_dir, raw_archive_dir=args.raw_archive)
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
                "dry_run": args.dry_run,
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
