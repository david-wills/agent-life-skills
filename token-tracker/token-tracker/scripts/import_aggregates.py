#!/usr/bin/env python3
"""Ingest a counts-only export produced by export_aggregates.py on another machine.

Rows are stored as synthetic `messages` entries -- one per (day, model, project) --
so every existing report and query works unchanged, with no second code path.
Ids are deterministic, so re-importing an updated export overwrites rather than
double-counts.

Known limitation: an aggregate host is day-granular. Its rows are timestamped at
noon PT, so a quota window that starts or ends mid-day attributes that whole day
to one side. Fine for daily and weekly reporting; not usable for the 5-hour drift
regression, which stays on fine-grained local data.

Usage:
  import_aggregates.py path/to/export.json   # import one file
  import_aggregates.py --watch               # import anything new in token_tracker.inbox
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib
import collect

PT = ZoneInfo("America/Los_Angeles")


def import_doc(con, doc, verbose=True):
    if doc.get("schema") not in (1, 2):
        raise SystemExit(f"unsupported export schema: {doc.get('schema')!r}")
    host = doc["host"]
    if host == lib.local_host():
        raise SystemExit(f"refusing to import: host {host!r} is the local machine, "
                         f"whose transcripts are already read directly")
    # A snapshot replaces that host entirely -- otherwise rows whose provider or
    # key changed between exports would linger as orphans.
    con.execute("DELETE FROM messages WHERE host = ?", (host,))
    con.execute("DELETE FROM attribution WHERE host = ?", (host,))
    msgs, attrs = [], {}
    for r in doc["rows"]:
        # Re-derive rather than trust the file: it corrects exports written
        # before local models were split out from OpenAI.
        provider = r.get("provider", "anthropic")
        if provider != "anthropic":
            provider = lib.classify_provider(r["model"])
        day, project = r["day"], r["project"]
        model = lib.normalize_model(r["model"]) if provider == "anthropic" else r["model"]
        entry = r.get("entrypoint") or "cli"
        noon = int(datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=PT).timestamp() * 1000)
        uuid = f"agg:{host}:{provider}:{day}:{model}:{project}:{entry}"
        sid = f"agg:{host}:{provider}:{day}:{project}"
        if provider == "anthropic":
            cost = lib.cost_usd(model, r["input"], r["output"], r["cache_read"],
                                r["cache_write_5m"], r["cache_write_1h"])
        else:
            price = lib.openai_prices(model)
            cost = 0.0 if not price else (
                r["input"] * price[0] + r["output"] * price[1]
                + r["cache_read"] * price[2]) / 1_000_000.0
        msgs.append((uuid, sid, project, noon, day, model, entry, 0,
                     r["input"], r["output"], r["cache_read"], r["cache_write_5m"],
                     r["cache_write_1h"], r.get("thinking", 0), cost, host, provider))
        kind = {"openai": "codex", "local": "local"}.get(provider, "terminal")
        label = {"openai": "codex", "local": f"local:{model}"}.get(
            provider, f"terminal:{collect.short_project(project)}")
        attrs[sid] = (sid, host, kind, label, None, None, "aggregate-export", "high")
    con.executemany("""INSERT OR REPLACE INTO messages
        (uuid, session_id, project, ts, day, model, entrypoint, is_sidechain,
         input_tokens, output_tokens, cache_read, cache_write_5m, cache_write_1h,
         thinking_tokens, cost_usd, host, provider)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", msgs)
    con.executemany("""INSERT OR REPLACE INTO attribution
        (session_id, host, kind, label, agent, job_id, method, confidence)
        VALUES (?,?,?,?,?,?,?,?)""", list(attrs.values()))
    rl = doc.get("codex_rate_limits")
    if rl:
        import collect_codex
        collect_codex.record_limits(
            con, int(datetime.fromisoformat(
                doc["generated_at"].replace("Z", "+00:00")).timestamp() * 1000), rl)
    con.commit()
    if verbose:
        days = {r["day"] for r in doc["rows"]}
        total = con.execute("SELECT ROUND(SUM(cost_usd),2) FROM messages WHERE host=?",
                            (host,)).fetchone()[0]
        print(f"imported host={host}: {len(msgs)} rows over {len(days)} days "
              f"(exported {doc.get('generated_at','?')})")
        print(f"  {host} now totals ${total} API-equivalent in the database")
    return len(msgs)


def watch(con, folder=None, verbose=True):
    """Import any counts-only export dropped into the inbox folder.

    The inbox is any folder both machines can reach -- a synced drive, a network
    share, a shared mount -- so nothing needs SSH, a key, or shell access in
    either direction. A missing folder (unmounted, machine off) is a silent skip.
    """
    folder = folder or lib.cfg("token_tracker.inbox", None)
    folder = os.path.expanduser(folder) if folder else None
    if not folder or not os.path.isdir(folder):
        return 0
    seen = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path,size,mtime FROM files")}
    imported = 0
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = seen.get(path)
        if prev and st.st_size == prev[0] and st.st_mtime == prev[1]:
            continue                      # unchanged since last import
        try:
            doc = json.load(open(path))
        except (OSError, json.JSONDecodeError) as e:
            if verbose:
                print(f"inbox: skipping {os.path.basename(path)} — {e}", file=sys.stderr)
            continue
        try:
            import_doc(con, doc, verbose=verbose)
        except SystemExit as e:
            if verbose:
                print(f"inbox: skipping {os.path.basename(path)} — {e}", file=sys.stderr)
            continue
        con.execute("INSERT OR REPLACE INTO files (path, size, mtime, offset) "
                    "VALUES (?,?,?,0)", (path, st.st_size, st.st_mtime))
        con.commit()
        imported += 1
    return imported


def main():
    ap = argparse.ArgumentParser(description="Import a counts-only export from another machine.")
    ap.add_argument("path", nargs="?", help="JSON file from export_aggregates.py")
    ap.add_argument("--watch", action="store_true",
                    help="import everything new in the token_tracker.inbox folder "
                         "(also the default when no path is given)")
    a = ap.parse_args()
    con = lib.connect()
    lib.init(con)
    if a.watch or not a.path:
        n = watch(con)
        print(f"inbox: imported {n} export(s)")
    else:
        import_doc(con, json.load(open(os.path.expanduser(a.path))))
    con.close()


if __name__ == "__main__":
    main()
