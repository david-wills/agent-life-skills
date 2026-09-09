#!/usr/bin/env python3
"""One-off: collapse rows that were one API call written as several records.

Claude Code writes a multi-block assistant response as one JSONL record per
content block, each repeating the SAME usage object. The collector counted
records, so every figure was inflated -- 415 records for 173 real calls in one
sampled session, about 2.4x.

The collector now dedupes on requestId, but rows already stored have no
requestId, and 98 source transcripts have since been pruned by Claude Code's
30-day cleanup, so a full re-ingest would lose real history. Instead, collapse
*consecutive* runs of identical usage within a session.

Safe because cache_read grows monotonically across real calls: call N+1 reads
everything call N read plus what call N wrote. Two consecutive real calls
therefore cannot share a usage signature unless nothing was appended between
them, which cannot happen -- the assistant's own output is always appended.
Verified against requestId ground truth before running.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

SIG = ("model", "input_tokens", "output_tokens", "cache_read",
       "cache_write_5m", "cache_write_1h")


def find_duplicates(con):
    doomed = []
    sessions = [r[0] for r in con.execute(
        "SELECT DISTINCT session_id FROM messages WHERE request_id IS NULL")]
    for sid in sessions:
        rows = con.execute(
            f"SELECT uuid, {', '.join(SIG)} FROM messages "
            f"WHERE session_id = ? AND request_id IS NULL ORDER BY ts", (sid,)).fetchall()
        prev = None
        for r in rows:
            sig = r[1:]
            if prev is not None and sig == prev:
                doomed.append(r[0])
            else:
                prev = sig
    return doomed


def main():
    con = lib.connect()
    lib.init(con)
    before = con.execute("SELECT COUNT(*), ROUND(SUM(cost_usd),2) FROM messages").fetchone()
    doomed = find_duplicates(con)
    if "--dry-run" in sys.argv:
        print(f"would delete {len(doomed):,} of {before[0]:,} rows")
        return
    con.executemany("DELETE FROM messages WHERE uuid = ?", [(u,) for u in doomed])
    con.commit()
    after = con.execute("SELECT COUNT(*), ROUND(SUM(cost_usd),2) FROM messages").fetchone()
    print(f"rows  {before[0]:,} -> {after[0]:,}  (removed {len(doomed):,})")
    print(f"cost  ${before[1]:,.2f} -> ${after[1]:,.2f}")
    con.close()


if __name__ == "__main__":
    main()
