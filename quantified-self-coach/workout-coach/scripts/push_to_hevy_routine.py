#!/usr/bin/env python3
"""PUT today's workout plan into the rolling Hevy routine "Today's Workout".

The coach proposes a session in Slack; this script mirrors the same plan onto
the phone as a Hevy routine the user can open and start. Single rolling routine
(Option 1): we keep one routine in the account and PUT-overwrite it each
session, so the routine list never clutters.

Inputs (JSON on stdin or via --plan-file):
    {
      "title": "Today's Workout — Mon 4/27 UB",  # optional; default auto-generated
      "notes": "Warmup: 5 min bike + 2 light bench sets. RPE 7–8.",  # optional
      "exercises": [
        {
          "name": "Bench Press (Barbell)",            # Hevy template title
          "sets": 3,
          "reps": "8-10",                              # range or single int
          "weight_lb": 125,                            # optional
          "rest_seconds": 90,                          # optional
          "notes": "last: 3/23, 125×9 — hold"          # optional
        },
        ...
      ]
    }

On first run the script bootstraps:
  - Pulls every exercise template (paginated), caches name→id at
    workout-coach/hevy-cache/templates.json.
  - Looks up an existing routine titled exactly "Today's Workout" — creates
    one if missing — caches its UUID at workout-coach/hevy-cache/routine.json.

Subsequent runs: read caches, resolve names, PUT.

Aliases: workout-coach/hevy-cache/aliases.json maps coach-friendly names to
Hevy's exact template title when they diverge. Edit freely — empty by
default. Unresolved names abort the push and print close matches.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2).
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


API_BASE = "https://api.hevyapp.com/v1"
ROLLING_ROUTINE_TITLE = "Today's Workout"  # legacy single-routine fallback (no slot)
TEMPLATES_PAGE_SIZE = 100  # API documented max
ROUTINES_PAGE_SIZE = 10

WORKSPACE = Path.home() / ".openclaw" / "workspace"
CACHE_DIR = WORKSPACE / "workout-coach" / "hevy-cache"
TEMPLATES_PATH = CACHE_DIR / "templates.json"
ROUTINE_PATH = CACHE_DIR / "routine.json"  # legacy single-routine cache
ALIASES_PATH = CACHE_DIR / "aliases.json"

# Slot-specific rolling routines: UB and LB each overwrite their own routine so
# an upper-body push never clobbers the lower-body plan on the phone (and vice
# versa). Each slot has a canonical title (used for first-run title lookup) and
# its own ID cache file.
SLOT_TITLES = {
    "UB": "Today's Workout — Upper Body",
    "LB": "Today's Workout — Lower Body",
}
SLOT_CACHE = {
    "UB": CACHE_DIR / "routine_ub.json",
    "LB": CACHE_DIR / "routine_lb.json",
}

LB_PER_KG = 2.2046226218


def _token() -> str:
    tok = os.getenv("HEVY_API_KEY") or os.getenv("HEVY_TOKEN")
    if tok:
        return tok
    from read_secret import read_secret  # type: ignore[import-not-found]
    val = read_secret("HEVY_API_KEY")
    if not val:
        raise SystemExit("Missing HEVY_API_KEY (env or 1Password OpenClaw vault).")
    return val


def _request(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = API_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"api-key": token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Hevy {method} {path} failed: HTTP {exc.code} {exc.reason}\n{detail}") from exc


def _fetch_paginated(token: str, path: str, key: str, page_size: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        payload = _request("GET", f"{path}{sep}page={page}&pageSize={page_size}", token)
        batch = payload.get(key) or []
        items.extend(batch)
        page_count = payload.get("page_count") or 0
        if page >= page_count or not batch:
            break
        page += 1
    return items


def load_templates(token: str, *, force_refresh: bool = False) -> dict[str, str]:
    """Return a name (lowercased) → exercise_template_id map.

    Caches the raw template list on disk; refreshes if missing or --refresh.
    """
    if TEMPLATES_PATH.exists() and not force_refresh:
        cached = json.loads(TEMPLATES_PATH.read_text())
        templates = cached.get("templates") or []
    else:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        templates = _fetch_paginated(token, "/exercise_templates", "exercise_templates", TEMPLATES_PAGE_SIZE)
        TEMPLATES_PATH.write_text(
            json.dumps(
                {"fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "templates": templates},
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
    return {t["title"].lower(): t["id"] for t in templates}


def load_aliases() -> dict[str, str]:
    if not ALIASES_PATH.exists():
        return {}
    return {k.lower(): v for k, v in json.loads(ALIASES_PATH.read_text()).items()}


def normalize_slot(plan: dict[str, Any]) -> str | None:
    """Return 'UB'/'LB' for the plan's slot, or None if it can't be determined.

    Prefers an explicit `slot` field; falls back to sniffing the title so older
    callers that only set a title (e.g. "… Mon 4/27 UB") still route correctly.
    """
    raw = str(plan.get("slot") or "").strip().upper()
    if raw in ("UB", "UPPER", "UPPER BODY", "UPPERBODY"):
        return "UB"
    if raw in ("LB", "LOWER", "LOWER BODY", "LOWERBODY"):
        return "LB"
    title = str(plan.get("title") or "").upper()
    if "UPPER" in title or " UB" in title or title.endswith("UB"):
        return "UB"
    if "LOWER" in title or " LB" in title or title.endswith("LB"):
        return "LB"
    return None


def _slot_config(slot: str | None) -> tuple[str, Path]:
    """Map a slot to its canonical routine title + ID cache path.

    Unknown/None slot falls back to the legacy single "Today's Workout" routine
    so nothing breaks for callers that don't pass a slot.
    """
    if slot in SLOT_TITLES:
        return SLOT_TITLES[slot], SLOT_CACHE[slot]
    return ROLLING_ROUTINE_TITLE, ROUTINE_PATH


def resolve_routine_id(
    token: str, templates: dict[str, str], *, slot: str | None = None, force_refresh: bool = False
) -> str:
    """Find the rolling routine for this slot, creating it if necessary.

    Hevy rejects empty-exercise creates, so seed with a single placeholder set
    against any template; the next push will overwrite the routine in full.
    """
    title, cache_path = _slot_config(slot)

    if cache_path.exists() and not force_refresh:
        cached = json.loads(cache_path.read_text())
        routine_id = cached.get("id")
        if routine_id:
            return routine_id

    routines = _fetch_paginated(token, "/routines", "routines", ROUTINES_PAGE_SIZE)
    for r in routines:
        if (r.get("title") or "").strip() == title:
            routine_id = r["id"]
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"id": routine_id, "title": title}, indent=2) + "\n")
            return routine_id

    if not templates:
        raise SystemExit("Cannot bootstrap rolling routine: no exercise templates loaded.")
    placeholder_id = next(iter(templates.values()))
    body = {
        "routine": {
            "title": title,
            "folder_id": None,
            "notes": "Rolling routine — overwritten each session by the workout coach.",
            "exercises": [
                {
                    "exercise_template_id": placeholder_id,
                    "rest_seconds": 60,
                    "notes": "placeholder — will be replaced on next push",
                    "sets": [{"type": "normal", "weight_kg": None, "reps": 1}],
                }
            ],
        }
    }
    created = _request("POST", "/routines", token, body)
    created_routines = created.get("routine") or []
    if isinstance(created_routines, dict):
        created_routines = [created_routines]
    if not created_routines:
        raise SystemExit(f"Routine create returned no routine object: {json.dumps(created)[:300]}")
    routine_id = created_routines[0]["id"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"id": routine_id, "title": title}, indent=2) + "\n")
    return routine_id


def resolve_template(name: str, templates: dict[str, str], aliases: dict[str, str]) -> str:
    key = name.strip().lower()
    if key in aliases:
        key = aliases[key].strip().lower()
    tid = templates.get(key)
    if tid:
        return tid
    suggestions = difflib.get_close_matches(key, templates.keys(), n=5, cutoff=0.5)
    suggest_txt = "\n".join(f"  - {s}" for s in suggestions) if suggestions else "  (none)"
    raise SystemExit(
        f"Unknown Hevy exercise: {name!r}.\nClose matches:\n{suggest_txt}\n"
        f"Fix the name in the plan or add an alias to {ALIASES_PATH}."
    )


def _has_unresolved(plan: dict[str, Any], templates: dict[str, str], aliases: dict[str, str]) -> bool:
    def _missing(raw: str) -> bool:
        key = raw.strip().lower()
        if not key:
            return False
        if key in aliases:
            key = aliases[key].strip().lower()
        return key not in templates

    for ex in plan.get("exercises") or []:
        if _missing(ex.get("name") or ex.get("exercise") or ""):
            return True
        for alt in ex.get("alts") or []:
            alt_name = alt if isinstance(alt, str) else (alt.get("name") or alt.get("exercise") or "")
            if _missing(alt_name):
                return True
    return False


def parse_reps(raw: str | int) -> dict[str, Any]:
    """Translate a reps spec into Hevy set fields.

    Accepts int, "10", or "8-10"/"8–10"/"8 to 10". Returns a dict with
    either {"reps": N} or {"rep_range": {"start": A, "end": B}}.
    """
    if isinstance(raw, int):
        return {"reps": raw}
    text = str(raw).strip().replace("–", "-").replace("—", "-")
    if "-" in text:
        a, b = (s.strip() for s in text.split("-", 1))
        return {"rep_range": {"start": int(a), "end": int(b)}}
    return {"reps": int(text)}


def lb_to_kg(weight_lb: float | int | None) -> float | None:
    if weight_lb is None:
        return None
    return round(float(weight_lb) / LB_PER_KG, 2)


def build_payload(plan: dict[str, Any], templates: dict[str, str], aliases: dict[str, str]) -> dict[str, Any]:
    title = plan.get("title") or _default_title()
    notes = plan.get("notes")
    exercises_in = plan.get("exercises") or []
    if not exercises_in:
        raise SystemExit("Plan has no exercises.")

    out_exercises: list[dict[str, Any]] = []
    for ex in exercises_in:
        name = ex.get("name") or ex.get("exercise")
        if not name:
            raise SystemExit(f"Exercise entry missing 'name': {ex}")
        template_id = resolve_template(name, templates, aliases)
        sets_count = int(ex.get("sets", 3))
        reps_fields = parse_reps(ex.get("reps", "8-10"))
        weight_kg = lb_to_kg(ex.get("weight_lb"))
        if weight_kg is None and "weight_kg" in ex:
            weight_kg = float(ex["weight_kg"])
        rest_seconds = ex.get("rest_seconds", 90)

        primary_set: dict[str, Any] = {"type": "normal", "weight_kg": weight_kg, **reps_fields}
        out_exercises.append(
            {
                "exercise_template_id": template_id,
                "rest_seconds": rest_seconds,
                "notes": ex.get("notes") or "",
                "sets": [dict(primary_set) for _ in range(sets_count)],
            }
        )

        # Mirror the FIRST alt (if any) as an immediately-following exercise so
        # the user can pick the primary OR the alt at the gym. Same sets/reps;
        # alt weight is optional — pass a dict ({"name": ..., "weight_lb": ...})
        # to seed it from the coach's per-exercise history lookup, or a bare
        # string to leave the weight blank for them to fill in.
        alts = ex.get("alts") or []
        if alts:
            first = alts[0]
            if isinstance(first, str):
                alt_name = first
                alt_weight_kg = None
            else:
                alt_name = first.get("name") or first.get("exercise")
                if not alt_name:
                    raise SystemExit(f"Alt entry missing 'name': {first}")
                alt_weight_kg = lb_to_kg(first.get("weight_lb"))
                if alt_weight_kg is None and "weight_kg" in first:
                    alt_weight_kg = float(first["weight_kg"])
            alt_template_id = resolve_template(alt_name, templates, aliases)
            alt_set: dict[str, Any] = {"type": "normal", "weight_kg": alt_weight_kg, **reps_fields}
            out_exercises.append(
                {
                    "exercise_template_id": alt_template_id,
                    "rest_seconds": rest_seconds,
                    "notes": f"ALT for {name} — do this OR {name}, not both.",
                    "sets": [dict(alt_set) for _ in range(sets_count)],
                }
            )

    routine_obj: dict[str, Any] = {"title": title, "exercises": out_exercises}
    if notes is not None:
        routine_obj["notes"] = notes
    return {"routine": routine_obj}


def _default_title() -> str:
    today = dt.date.today()
    return f"{ROLLING_ROUTINE_TITLE} — {today.strftime('%a %-m/%-d')}"


def push(plan: dict[str, Any], *, refresh: bool = False, dry_run: bool = False) -> dict[str, Any]:
    token = _token()
    templates = load_templates(token, force_refresh=refresh)
    aliases = load_aliases()

    # If any exercise name doesn't resolve against the cached template list,
    # refresh once before erroring — picks up custom Hevy exercises the user adds
    # in the app.
    if not refresh and _has_unresolved(plan, templates, aliases):
        templates = load_templates(token, force_refresh=True)

    payload = build_payload(plan, templates, aliases)
    slot = normalize_slot(plan)

    if dry_run:
        return {"dry_run": True, "slot": slot, "payload": payload}

    routine_id = resolve_routine_id(token, templates, slot=slot, force_refresh=refresh)
    result = _request("PUT", f"/routines/{routine_id}", token, payload)
    return {"routine_id": routine_id, "slot": slot, "title": payload["routine"]["title"], "result": result}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan-file", help="Path to plan JSON. Defaults to stdin.")
    p.add_argument("--refresh", action="store_true", help="Refresh template + routine caches.")
    p.add_argument("--dry-run", action="store_true", help="Build payload, print, but don't PUT.")
    p.add_argument("--bootstrap", action="store_true", help="Just bootstrap caches and exit (no plan needed).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.bootstrap:
        token = _token()
        templates = load_templates(token, force_refresh=args.refresh)
        print(f"Templates cached: {len(templates)} → {TEMPLATES_PATH}")
        for slot in ("UB", "LB"):
            routine_id = resolve_routine_id(token, templates, slot=slot, force_refresh=args.refresh)
            print(f"{slot} rolling routine ID: {routine_id} → {SLOT_CACHE[slot]}")
        if not ALIASES_PATH.exists():
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            ALIASES_PATH.write_text("{}\n")
            print(f"Created empty alias map: {ALIASES_PATH}")
        return 0

    if args.plan_file:
        plan = json.loads(Path(args.plan_file).read_text())
    else:
        if sys.stdin.isatty():
            raise SystemExit("Provide --plan-file or pipe plan JSON to stdin. (Use --bootstrap to seed caches.)")
        plan = json.loads(sys.stdin.read())

    summary = push(plan, refresh=args.refresh, dry_run=args.dry_run)
    if args.dry_run:
        print(f"# slot: {summary.get('slot')}")
        print(json.dumps(summary["payload"], indent=2))
    else:
        print(f"PUT routine {summary['routine_id']} (slot {summary.get('slot')}): {summary['title']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
