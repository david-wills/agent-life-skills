#!/usr/bin/env python3
"""Which of the upcoming Thursdays are free for dinner? JSON to stdout."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from calendar_lib import free_thursdays  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=4)
    args = ap.parse_args()
    print(json.dumps({"thursdays": free_thursdays(args.weeks)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
