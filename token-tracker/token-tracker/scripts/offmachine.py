#!/usr/bin/env python3
"""Detect and size Claude usage that happened on machines this tracker can't see.

Quota utilization from the OAuth endpoint is account-wide; transcripts are only
from this Mac. So the derived "$ per 1% of quota" silently mixes both, and any
off-machine usage makes every workflow's quota share read too high.

Two independent signals, neither of which needs access to the other machine:

  1. Scoped buckets -- a model burning weekly quota with zero local messages can
     only have run elsewhere. Coarse but immediate and unambiguous.
  2. Regression on the 5-hour window -- fit
         d_utilization = k * d_local_spend + r * d_hours
     across consecutive samples inside one 5h window. `k` converts local dollars
     to quota percent; `r` is the residual drift that local spend can't explain,
     i.e. the other machines' burn rate. Needs a few hours of samples.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib

MIN_INTERVALS = 8


def unseen_models(con):
    """Models with weekly quota burn but no local messages -> definitely off-machine."""
    row = con.execute("""SELECT scoped_json FROM quota_samples
                         WHERE scoped_json IS NOT NULL ORDER BY ts DESC LIMIT 1""").fetchone()
    if not row or not row[0]:
        return {}
    # every host we have data for -- importing a machine's export should stop its
    # models from reading as "off-machine"
    local = {m.lower() for (m,) in con.execute(
        "SELECT DISTINCT model FROM messages WHERE provider='anthropic'")}
    out = {}
    for name, pct in json.loads(row[0]).items():
        if pct and pct > 0 and not any(name.lower() in m for m in local):
            out[name] = pct
    return out


def intervals(con):
    """Consecutive quota samples inside one 5h window, paired with local spend."""
    rows = con.execute("""SELECT ts, five_hour_pct, five_hour_reset FROM quota_samples
                          WHERE five_hour_pct IS NOT NULL ORDER BY ts""").fetchall()
    out = []
    for (t0, u0, r0), (t1, u1, r1) in zip(rows, rows[1:]):
        if r0 != r1 or u1 < u0:
            continue                      # window rolled over; delta is meaningless
        dt = (t1 - t0) / 3_600_000.0
        if dt <= 0 or dt > 2.0:           # a long gap (sleep) hides too much
            continue
        # Local host only: an aggregate-imported host is timestamped at noon, so
        # including it would dump a whole day of spend into one 30-minute bucket.
        spend = con.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM messages "
            "WHERE ts > ? AND ts <= ? AND host = ? AND provider = 'anthropic'",
            (t0, t1, lib.LOCAL_HOST)).fetchone()[0]
        out.append((u1 - u0, spend, dt))
    return out


def fit(con):
    """Least squares for (k, r). Returns None until enough intervals exist."""
    data = intervals(con)
    if len(data) < MIN_INTERVALS:
        return None, len(data)
    sss = sum(s * s for _, s, _ in data)
    sst = sum(s * t for _, s, t in data)
    stt = sum(t * t for _, _, t in data)
    ssu = sum(s * u for u, s, _ in data)
    stu = sum(t * u for u, _, t in data)
    det = sss * stt - sst * sst
    if abs(det) < 1e-12:
        return None, len(data)
    k = (ssu * stt - stu * sst) / det          # quota % per local dollar
    r = (sss * stu - sst * ssu) / det          # quota % per hour, unexplained
    return {"pct_per_dollar": k, "offmachine_pct_per_hour": max(r, 0.0),
            "usd_per_pct": (1.0 / k) if k > 1e-9 else None,
            "n": len(data)}, len(data)


def summary(con):
    """One dict the report can render without repeating any of this logic."""
    unseen = unseen_models(con)
    f, n = fit(con)
    return {"unseen_models": unseen, "fit": f, "n_intervals": n}


def main():
    con = lib.connect()
    lib.init(con)
    s = summary(con)
    print("models burning quota with no local usage:", s["unseen_models"] or "none")
    if s["fit"]:
        f = s["fit"]
        print(f"local rate: ${f['usd_per_pct']:.2f} per 1% of 5h quota")
        print(f"off-machine drift: {f['offmachine_pct_per_hour']:.2f}%/hour  (n={f['n']})")
    else:
        print(f"regression: need {MIN_INTERVALS} intervals, have {s['n_intervals']} "
              f"-- accumulating (samples every 30 min)")
    con.close()


if __name__ == "__main__":
    main()
