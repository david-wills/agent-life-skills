#!/usr/bin/env python3
"""Ingest Codex (OpenAI) token usage and its own rate-limit percentages.

Codex writes rollout files that carry both halves of what this tracker needs:
per-turn `last_token_usage` counts and a `rate_limits` block with 5-hour and
weekly used_percent. Structurally the same shape as Claude transcripts plus the
OAuth usage endpoint -- just already in one file.

Two roots, because Codex driven by the OpenClaw gateway does not share the CLI's home:
  ~/.codex/sessions                          the user's own terminal Codex
  <openclaw agents>/*/agent/codex-home       each agent's ACP sessions (absent without OpenClaw)

Cost is only computed when token_tracker.openai_prices is configured; otherwise
rows carry tokens and quota but no dollars, rather than invented ones.

Usage:
  collect_codex.py            # ingest new rollouts and print the totals
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib



def iso_ms(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def roots():
    patterns = [
        (os.path.expanduser("~/.codex/sessions"), "terminal"),
        (os.path.join(lib.openclaw_paths()["agents"], "*", "agent", "codex-home"), "openclaw"),
    ]
    out = []
    for pattern, kind in patterns:
        for d in (glob.glob(pattern) if "*" in pattern else [pattern]):
            if os.path.isdir(d):
                agent = d.split("/agents/")[1].split("/")[0] if "/agents/" in d else None
                out.append((d, kind, agent))
    return out


def cost(model, inp, out, cached):
    p = lib.openai_prices(model)
    if not p:
        return 0.0
    pin, pout, pcached = p
    # `input_tokens` from Codex is inclusive of the cached portion
    fresh = max(inp - cached, 0)
    return (fresh * pin + cached * pcached + out * pout) / 1_000_000.0


def ingest(con, verbose=True):
    seen = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT path,size,mtime,offset FROM files")}
    rows, limits, files_touched = [], None, 0
    host = lib.local_host()

    for root, kind, agent in roots():
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            prev = seen.get(path)
            if prev and st.st_size == prev[0] and st.st_mtime == prev[1]:
                continue
            files_touched += 1
            sid = os.path.basename(path).replace(".jsonl", "")
            model, n = None, 0
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
                        model = ((d.get("payload") or {}).get("model")
                                 or (d.get("payload") or {}).get("turn_context", {}).get("model")
                                 or model)
                    if "token_count" not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = d.get("payload") or {}
                    info = payload.get("info") or {}
                    u = info.get("last_token_usage") or {}
                    if not u:
                        continue
                    ts = iso_ms(d.get("timestamp"))
                    if ts is None:
                        continue
                    rl = payload.get("rate_limits")
                    if rl:
                        limits = (ts, rl)
                    inp = u.get("input_tokens", 0)
                    cached = u.get("cached_input_tokens", 0)
                    out = u.get("output_tokens", 0)
                    reasoning = u.get("reasoning_output_tokens", 0)
                    if not (inp or out):
                        continue
                    m = model or "gpt-unknown"
                    day = datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(lib.day_tz()).strftime("%Y-%m-%d")
                    n += 1
                    rows.append((
                        f"codex:{sid}:{n}", sid, f"codex-{kind}" + (f"-{agent}" if agent else ""),
                        ts, day, m, "codex", 0,
                        max(inp - cached, 0), out, cached, 0, 0, reasoning,
                        cost(m, inp, out, cached) if lib.classify_provider(m) == "openai" else 0.0,
                        host, lib.classify_provider(m),
                    ))
            con.execute("INSERT OR REPLACE INTO files (path, size, mtime, offset) VALUES (?,?,?,?)",
                        (path, st.st_size, st.st_mtime, 0))

    if rows:
        con.executemany("""INSERT OR REPLACE INTO messages
            (uuid, session_id, project, ts, day, model, entrypoint, is_sidechain,
             input_tokens, output_tokens, cache_read, cache_write_5m, cache_write_1h,
             thinking_tokens, cost_usd, host, provider)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        sids = {r[1] for r in rows}
        con.executemany("""INSERT OR REPLACE INTO attribution
            (session_id, host, kind, label, agent, job_id, method, confidence)
            VALUES (?,?,?,?,?,?,?,?)""",
            [(s, host, "codex", "codex", None, None, "codex-rollout", "high")
             for s in sids])
    if limits:
        record_limits(con, *limits)
    con.commit()
    if verbose:
        print(f"codex: {files_touched} rollouts read, {len(rows)} turn records")
    return len(rows)


def record_limits(con, ts, rl):
    """Codex ships its own 5h/weekly percentages inside the rollout."""
    primary = rl.get("primary") or {}
    secondary = rl.get("secondary") or {}
    con.execute("""INSERT OR REPLACE INTO quota_samples
        (ts, five_hour_pct, five_hour_reset, seven_day_pct, seven_day_reset,
         opus_pct, raw_json, scoped_json, provider)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (ts, primary.get("used_percent"),
         _reset(primary.get("resets_at")), secondary.get("used_percent"),
         _reset(secondary.get("resets_at")), None, json.dumps(rl), None, "openai"))


def _reset(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.parse_args()
    con = lib.connect()
    lib.init(con)
    try:
        ingest(con)
    except RuntimeError as e:
        print(f"codex: {e}", file=sys.stderr)
        raise SystemExit(1)
    r = con.execute("""SELECT COUNT(*), SUM(input_tokens), SUM(cache_read), SUM(output_tokens),
                              MIN(day), MAX(day) FROM messages WHERE provider='openai'""").fetchone()
    if not r[0]:
        print("db holds no codex turns (no rollout files found)")
    else:
        print(f"db now holds {r[0]} codex turns: {r[1]:,} fresh in / {r[2]:,} cached / "
              f"{r[3]:,} out, {r[4]} .. {r[5]}")
    q = con.execute("""SELECT five_hour_pct, seven_day_pct FROM quota_samples
                       WHERE provider='openai' ORDER BY ts DESC LIMIT 1""").fetchone()
    if q:
        print(f"latest codex quota: 5h {q[0]}%  weekly {q[1]}%")
    con.close()


if __name__ == "__main__":
    main()
