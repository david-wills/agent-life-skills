#!/usr/bin/env python3
"""Snapshot real Claude subscription quota utilization.

Reads the same endpoint Claude Code's /usage command uses, with the OAuth token
from the macOS keychain. This is the only source of *actual* quota consumption --
token counts alone can't tell you what fraction of the subscription you've burned,
because Anthropic's limits aren't published as token numbers.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def fetch():
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {lib.oauth_token()}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def scoped_buckets(data):
    """Per-model weekly limits from the `limits` array, e.g. {"Fable": 8.0}.

    These are separate from weekly_all and are the only place model-scoped burn
    shows up -- the top-level seven_day_* fields are null on this plan.
    """
    out = {}
    for lim in data.get("limits") or []:
        scope = lim.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name")
        if model and lim.get("percent") is not None:
            out[model] = float(lim["percent"])
    return out


def record(con, data):
    fh = data.get("five_hour") or {}
    sd = data.get("seven_day") or {}
    op = data.get("seven_day_opus") or {}
    row = (int(time.time() * 1000), fh.get("utilization"), fh.get("resets_at"),
           sd.get("utilization"), sd.get("resets_at"), op.get("utilization"),
           json.dumps(data), json.dumps(scoped_buckets(data)), "anthropic")
    # Name the columns: this table gains columns over time, and a positional
    # insert silently breaks the moment one is added.
    con.execute("""INSERT OR REPLACE INTO quota_samples
        (ts, five_hour_pct, five_hour_reset, seven_day_pct, seven_day_reset,
         opus_pct, raw_json, scoped_json, provider)
        VALUES (?,?,?,?,?,?,?,?,?)""", row)
    con.commit()
    return row


def latest(con):
    r = con.execute("""SELECT ts, five_hour_pct, five_hour_reset, seven_day_pct,
                              seven_day_reset, opus_pct FROM quota_samples
                       ORDER BY ts DESC LIMIT 1""").fetchone()
    if not r:
        return None
    return dict(zip(("ts", "five_hour_pct", "five_hour_reset", "seven_day_pct",
                     "seven_day_reset", "opus_pct"), r))


def detect_reset(con):
    """Utilization falling without the window rolling = an out-of-band change.

    Happened on 2026-09-04 (weekly 24% -> 1%, resets_at unchanged) and went
    unnoticed for hours. Worth telling the user about, because it silently
    invalidates the quota calibration until usage rebuilds.
    """
    rows = con.execute(
        """SELECT ts, seven_day_pct, seven_day_reset FROM quota_samples
           WHERE provider = 'anthropic' AND seven_day_pct IS NOT NULL
           ORDER BY ts DESC LIMIT 2""").fetchall()
    if len(rows) < 2:
        return None
    (t1, p1, r1), (t0, p0, r0) = rows
    if p1 >= p0 - 0.5:
        return None
    same_window = (r0 or "")[:19] == (r1 or "")[:19]
    if not same_window:
        return None                       # ordinary weekly roll
    already = con.execute(
        "SELECT 1 FROM alerts WHERE kind='quota-reset' AND ref_ts=?", (t1,)).fetchone()
    if already:
        return None
    return {"ts": t1, "from_pct": p0, "to_pct": p1, "prev_ts": t0,
            "resets_at": r1}


def mark_alerted(con, kind, ref_ts):
    con.execute("INSERT OR REPLACE INTO alerts (kind, ref_ts, sent_at) VALUES (?,?,?)",
                (kind, ref_ts, int(time.time() * 1000)))
    con.commit()


def main():
    con = lib.connect()
    lib.init(con)
    row = record(con, fetch())
    print(f"5h={row[1]}%  7d={row[3]}%  scoped={row[7]}  (7d resets {row[4]})")
    con.close()


if __name__ == "__main__":
    main()
