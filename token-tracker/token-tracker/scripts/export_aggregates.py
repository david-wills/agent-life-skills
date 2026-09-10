#!/usr/bin/env python3
"""Summarise this machine's Claude token usage into a small JSON file.

Covers both Claude transcripts and Codex rollouts in one pass.

Run this ON the machine being measured. It is deliberately standalone -- stdlib
only, no imports from the rest of the skill -- so it can be copied to any Mac and
run without installing anything.

Why this exists: the alternative is giving the tracker an SSH key, which grants
full shell access to the whole machine. This reads transcripts locally and emits
only counts. No prompts, no responses, no code, no file contents, no session ids
ever leave the machine -- just per (day, model, project) token totals. A project
name is the transcript directory name, which embeds the path (and usually your
username): --redact-projects replaces each with a stable short hash, so the
per-project breakdown survives and the path does not.

Days are cut in this machine's local zone unless --timezone names another; use
the same zone the importing tracker uses (token_tracker.timezone there).

    python3 export_aggregates.py --host laptop --out ~/Desktop/laptop-tokens.json

Move the result to the machine running the tracker however you like (a shared
folder, AirDrop, a copy-paste), then run import_aggregates.py there -- or drop it
in the folder that machine's `token_tracker.inbox` points at and it is picked up
on the next half-hourly sample.
"""
import argparse
import glob
import hashlib
import json
import os
import socket
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SCHEMA = 2


def iso_to_ms(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


OPENAI_PREFIXES = ("gpt", "o1", "o3", "o4", "chatgpt", "codex", "davinci")


def classify_provider(model):
    """Rollouts record no provider, and local models reach Codex-style harnesses
    through an OpenAI-compatible endpoint -- so classify by model name."""
    m = (model or "").lower()
    if "claude" in m:
        return "anthropic"
    if m.startswith(OPENAI_PREFIXES) or "codex" in m:
        return "openai"
    return "local"


# The second root only exists where the OpenClaw gateway drives Codex; absent otherwise.
CODEX_ROOTS = [
    "~/.codex/sessions",
    "~/.openclaw/agents/*/agent/codex-home",
]


def collect_codex(patterns=None, tz=None):
    """Codex rollouts: per-turn token counts plus the plan's own rate limits.

    Same counts-only contract as the Claude side -- nothing but numbers leaves
    the machine. Codex reports input_tokens inclusive of cached_input_tokens, so
    the cached part is subtracted out to avoid double counting.
    """
    agg = defaultdict(lambda: dict(input=0, output=0, cache_read=0, cache_write_5m=0,
                                   cache_write_1h=0, thinking=0, messages=0))
    sessions = defaultdict(set)
    limits = None
    for pattern in (patterns or CODEX_ROOTS):
        pattern = os.path.expanduser(pattern)
        for root in (glob.glob(pattern) if "*" in pattern else [pattern]):
            if not os.path.isdir(root):
                continue
            for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
                model = None
                try:
                    fh = open(path, errors="replace")
                except OSError:
                    continue
                with fh:
                    for line in fh:
                        if '"model"' in line and model is None:
                            try:
                                d = json.loads(line)
                            except json.JSONDecodeError:
                                d = {}
                            pl = d.get("payload") or {}
                            model = pl.get("model") or (pl.get("turn_context") or {}).get("model") or model
                        if "token_count" not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        pl = d.get("payload") or {}
                        u = (pl.get("info") or {}).get("last_token_usage") or {}
                        if not u:
                            continue
                        ts = iso_to_ms(d.get("timestamp"))
                        if ts is None:
                            continue
                        if pl.get("rate_limits"):
                            limits = pl["rate_limits"]
                        day = datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
                        cached = u.get("cached_input_tokens", 0)
                        key = (day, model or "gpt-unknown", "codex", "codex")
                        a = agg[key]
                        a["input"] += max(u.get("input_tokens", 0) - cached, 0)
                        a["cache_read"] += cached
                        a["output"] += u.get("output_tokens", 0)
                        a["thinking"] += u.get("reasoning_output_tokens", 0)
                        a["messages"] += 1
                        sessions[key].add(path)
    rows = []
    for (day, model, project, entry), a in sorted(agg.items()):
        rows.append(dict(day=day, model=model, project=project, entrypoint=entry,
                         provider=classify_provider(model),
                         sessions=len(sessions[(day, model, project, entry)]), **a))
    return rows, limits


def collect(projects_dir, tz=None, redact=False):
    agg = defaultdict(lambda: dict(input=0, output=0, cache_read=0, cache_write_5m=0,
                                   cache_write_1h=0, thinking=0, messages=0))
    sessions = defaultdict(set)
    for path in glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True):
        project = path.rsplit("/projects/", 1)[-1].split("/")[0]
        if redact:
            project = hashlib.sha256(project.encode("utf-8")).hexdigest()[:12]
        # One API response spans several records, each repeating the same usage.
        seen_requests = set()
        try:
            fh = open(path, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                model = msg.get("model") or ""
                if not u or not model or model.startswith("<"):
                    continue
                ts = iso_to_ms(d.get("timestamp"))
                if ts is None:
                    continue
                req = d.get("requestId") or msg.get("id")
                if req:
                    if req in seen_requests:
                        continue
                    seen_requests.add(req)
                day = datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
                key = (day, model, project, d.get("entrypoint") or "")
                cc = u.get("cache_creation") or {}
                cw5 = cc.get("ephemeral_5m_input_tokens", 0)
                cw1 = cc.get("ephemeral_1h_input_tokens", 0)
                if not cc:
                    cw5 = u.get("cache_creation_input_tokens", 0)
                a = agg[key]
                a["input"] += u.get("input_tokens", 0)
                a["output"] += u.get("output_tokens", 0)
                a["cache_read"] += u.get("cache_read_input_tokens", 0)
                a["cache_write_5m"] += cw5
                a["cache_write_1h"] += cw1
                a["thinking"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
                a["messages"] += 1
                sessions[key].add(d.get("sessionId") or path)
    rows = []
    for (day, model, project, entry), a in sorted(agg.items()):
        rows.append(dict(day=day, model=model, project=project, entrypoint=entry,
                         provider="anthropic",
                         sessions=len(sessions[(day, model, project, entry)]), **a))
    return rows


def main():
    ap = argparse.ArgumentParser(description="Summarise this machine's token usage as counts-only JSON.")
    ap.add_argument("--host", default=None,
                    help="short label for this machine (default: its hostname); must differ "
                         "from token_tracker.host on the importing machine")
    ap.add_argument("--projects", default="~/.claude/projects",
                    help="Claude Code transcript root (default: %(default)s)")
    ap.add_argument("--out", default="~/Desktop/claude-tokens.json",
                    help="where to write the export (default: %(default)s)")
    ap.add_argument("--no-codex", action="store_true", help="skip Codex rollouts")
    ap.add_argument("--timezone", default=None,
                    help="IANA zone that defines a day, e.g. Europe/Berlin (default: this machine's local zone)")
    ap.add_argument("--redact-projects", action="store_true",
                    help="replace project names with a stable 12-hex hash (they embed the transcript path)")
    a = ap.parse_args()

    tz = ZoneInfo(a.timezone) if a.timezone else None
    rows = collect(os.path.expanduser(a.projects), tz=tz, redact=a.redact_projects)
    codex_rows, codex_limits = ([], None) if a.no_codex else collect_codex(tz=tz)
    rows += codex_rows
    doc = {
        "schema": SCHEMA,
        "host": a.host or socket.gethostname().split(".")[0],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "codex_rate_limits": codex_limits,
        "rows": rows,
    }
    out = os.path.expanduser(a.out)
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=1)
    tok = sum(r["input"] + r["output"] + r["cache_read"] + r["cache_write_5m"]
              + r["cache_write_1h"] for r in rows)
    days = {r["day"] for r in rows}
    print(f"wrote {out}")
    byprov = {}
    for r in rows:
        byprov[r.get("provider", "anthropic")] = byprov.get(r.get("provider", "anthropic"), 0) + 1
    print(f"  host={doc['host']}  {len(rows)} rows  {len(days)} days  {tok:,} tokens")
    print(f"  providers: {byprov}")
    print(f"  contains counts only -- no prompts, responses, code, or session ids"
          + ("; project names redacted" if a.redact_projects else "; project names included"))


if __name__ == "__main__":
    main()
