#!/usr/bin/env python3
"""Build the daily token/cost/quota report and post it to Discord.

Usage:
  report.py                 # today so far, print + post
  report.py --day 2026-09-03
  report.py --week          # the current 7-day quota window
  report.py --no-post       # print only
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib
import collect
import offmachine
import quota as quota_mod

PT = ZoneInfo("America/Los_Angeles")
SERVICE_ENV = Path.home() / ".openclaw/service-env/ai.openclaw.gateway.env"


# ---------------------------------------------------------------- quota model

def segment_rates(con, lo, hi):
    """Derive $-per-1% separately for each monotonic run of utilization.

    Utilization resets mid-window (credits, plan changes), so the window splits
    into segments. Each is an independent estimate, and comparing them is the
    only honest check on the conversion: on 2026-09-04 two segments of the same
    week gave $29.66 and $69.81 per point -- a 2.4x spread. The quota is not
    denominated in dollars, so the rate moves with model mix. A single point
    estimate reads far more precise than the thing being measured.
    """
    rows = con.execute(
        """SELECT ts, seven_day_pct FROM quota_samples
           WHERE provider = 'anthropic' AND seven_day_pct IS NOT NULL
             AND ts >= ? AND ts <= ? ORDER BY ts""", (lo, hi)).fetchall()
    if not rows:
        return []
    # The window opens at 0%, which anchors the first segment even when sampling
    # started mid-week. Without it the pre-reset run looks like a 1pp blip and
    # the only surviving estimate is whichever segment happens to be sampled.
    rows = [(lo, 0.0)] + rows
    segments, start = [], 0
    for i in range(1, len(rows)):
        if rows[i][1] < rows[i - 1][1] - 0.5:
            segments.append(rows[start:i])
            start = i
    segments.append(rows[start:])

    rates = []
    for seg in segments:
        if len(seg) < 2:
            continue
        (t0, p0), (t1, p1) = seg[0], seg[-1]
        d = p1 - p0
        if d < 2:                      # integer percents: under 2pp is noise
            continue
        spend = con.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM messages "
            "WHERE ts > ? AND ts <= ? AND provider = 'anthropic'",
            (t0, t1)).fetchone()[0]
        if spend > 0:
            rates.append(spend / d)
    return sorted(rates)


def quota_calibration(con, q):
    """Dollars of API-equivalent spend per 1% of the weekly subscription quota.

    Anthropic never publishes the weekly limit as a token or dollar number, so we
    derive it from measured spend against reported utilization.

    The subtlety is that reported utilization is not monotonic within a window.
    On 2026-09-04 it fell from 24% to 1% with `resets_at` unchanged -- an
    out-of-band credit or plan adjustment, not a window roll. Dividing the whole
    window's spend by the post-reset 1% would have overstated the rate ~23x and
    made every workflow's quota share meaningless. So we calibrate only over the
    latest monotonic segment: spend since the last drop, against the rise since
    that same point.

    Returns (usd_per_pct, window_spend, (start, end), reset_detected).
    """
    if not q or not q.get("seven_day_reset") or q.get("seven_day_pct") is None:
        return None, None, None, False
    end = datetime.fromisoformat(q["seven_day_reset"].replace("Z", "+00:00"))
    start = end - timedelta(days=7)
    lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def spend_between(a, b):
        # Account-wide quota, so every host counts -- but only Anthropic models.
        return con.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM messages "
            "WHERE ts >= ? AND ts < ? AND provider = 'anthropic'", (a, b)).fetchone()[0]

    window_spend = spend_between(lo, hi)

    samples = con.execute(
        """SELECT ts, seven_day_pct FROM quota_samples
           WHERE provider = 'anthropic' AND seven_day_pct IS NOT NULL
             AND ts >= ? AND ts <= ? ORDER BY ts""", (lo, hi)).fetchall()

    seg_ts, seg_pct, reset_detected = lo, 0.0, False
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        if p1 < p0 - 0.5:            # utilization fell without the window rolling
            seg_ts, seg_pct, reset_detected = t1, p1, True

    now_pct = q["seven_day_pct"]
    d_pct = now_pct - seg_pct
    if d_pct <= 0:
        return None, window_spend, (start, end), reset_detected
    seg_spend = spend_between(seg_ts, hi)
    if seg_spend <= 0:
        return None, window_spend, (start, end), reset_detected
    return seg_spend / d_pct, window_spend, (start, end), reset_detected


# ---------------------------------------------------------------- queries

def totals(con, where, args):
    r = con.execute(f"""
        SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
               COALESCE(SUM(cache_read),0), COALESCE(SUM(cache_write_5m+cache_write_1h),0),
               COALESCE(SUM(thinking_tokens),0), COALESCE(SUM(cost_usd),0)
        FROM messages WHERE {where}""", args).fetchone()
    return dict(zip(("input", "output", "cache_read", "cache_write", "thinking", "cost"), r))


def by_model(con, where, args):
    return con.execute(f"""
        SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cache_read),
               SUM(cache_write_5m+cache_write_1h), SUM(cost_usd)
        FROM messages WHERE {where} GROUP BY model ORDER BY 6 DESC""", args).fetchall()


def by_host(con, where, args):
    return con.execute(f"""
        SELECT host, COUNT(DISTINCT session_id),
               SUM(input_tokens+cache_read+cache_write_5m+cache_write_1h),
               SUM(output_tokens), SUM(cost_usd)
        FROM messages WHERE {where} GROUP BY host ORDER BY 5 DESC""", args).fetchall()


def by_workflow(con, where, args, limit=12):
    return con.execute(f"""
        SELECT a.label, a.kind, m.host, COUNT(DISTINCT m.session_id),
               SUM(m.input_tokens+m.cache_read+m.cache_write_5m+m.cache_write_1h),
               SUM(m.output_tokens), SUM(m.cost_usd)
        FROM messages m JOIN attribution a USING(session_id)
        WHERE {where} GROUP BY 1,2,3 ORDER BY 7 DESC LIMIT {limit}""", args).fetchall()


# ---------------------------------------------------------------- formatting

def human(n):
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def build(con, day=None, week=False):
    q = quota_mod.latest(con)
    usd_per_pct, window_spend, window, quota_reset = quota_calibration(con, q)

    if week and window:
        where = "ts >= ? AND ts < ?"
        args = (int(window[0].timestamp() * 1000), int(window[1].timestamp() * 1000))
        title = f"Token report — 7-day quota window (through {datetime.now(PT):%b %-d %-I:%M%p})"
    else:
        day = day or datetime.now(PT).strftime("%Y-%m-%d")
        where, args = "day = ?", (day,)
        label = "today" if day == datetime.now(PT).strftime("%Y-%m-%d") else day
        title = f"Token report — {day}" + (" (so far)" if label == "today" else "")

    where_claude = f"({where}) AND provider='anthropic'"
    where_codex = f"({where}) AND provider='openai'"
    where_local = f"({where}) AND provider='local'"
    t = totals(con, where_claude, args)
    total_in = t["input"] + t["cache_read"] + t["cache_write"]

    def pct_of_week(usd):
        return f"≈{usd/usd_per_pct:.2f}% wk" if usd_per_pct else "n/a"

    L = [f"**{title}**", ""]
    L.append("__Claude (subscription quota)__")

    # headline
    hosts_seen = [h for h, *_ in by_host(con, where_claude, args)]
    scope = "all machines" if len(hosts_seen) > 1 else (hosts_seen[0] if hosts_seen else "")
    L.append(f"**Total:** {human(total_in + t['output'])} tokens · "
             f"**${t['cost']:.2f}** API-equivalent"
             + (f" ({scope})" if scope else "")
             + (f" · ≈{t['cost']/usd_per_pct:.1f}% of a weekly allowance"
                if usd_per_pct else ""))
    L.append(f"· in {human(t['input'])} · out {human(t['output'])} "
             f"(thinking {human(t['thinking'])}) · cache read {human(t['cache_read'])} "
             f"· cache write {human(t['cache_write'])}")
    if t["cache_read"]:
        naive = t["cache_read"] / max(total_in, 1)
        L.append(f"· {naive*100:.0f}% of input was cache reads (billed at 10%)")
    L.append("")

    # live quota
    if q:
        L.append(f"**Subscription quota (live):** 5-hour **{q['five_hour_pct']:.0f}%** · "
                 f"weekly **{q['seven_day_pct']:.0f}%**")
        if window:
            L.append(f"· window spend ${window_spend:.2f} → "
                     f"1% of weekly quota ≈ ${usd_per_pct:.2f}" if usd_per_pct else
                     f"· window spend ${window_spend:.2f}")
            L.append(f"· **{q['seven_day_pct']:.0f}% is what you have actually consumed** — "
                     f"the ≈% figures below convert spend at the derived rate and do "
                     f"not add up to it")
            rates = segment_rates(con, int(window[0].timestamp() * 1000),
                                  int(window[1].timestamp() * 1000))
            if len(rates) > 1 and rates[-1] > rates[0] * 1.5:
                L.append(f"· ⚠︎ that rate is **not stable**: independent segments of this "
                         f"window imply ${rates[0]:,.0f}–${rates[-1]:,.0f} per 1%. The quota "
                         f"is not dollar-denominated, so the conversion moves with model "
                         f"mix — treat ≈% as an order of magnitude, not a measurement.")
        if quota_reset:
            L.append("· ⚠︎ weekly utilization was reset mid-window (credit or plan "
                     "change). The rate is calibrated on usage since that reset, so "
                     "window totals above it are not comparable to the live figure.")
        if q.get("seven_day_reset"):
            reset = datetime.fromisoformat(q["seven_day_reset"].replace("Z", "+00:00")).astimezone(PT)
            L.append(f"· weekly resets {reset:%a %b %-d %-I:%M%p}")
        L.append("")

    # machines -- only worth a section once more than one is feeding the DB
    machines = by_host(con, where_claude, args)
    if len(machines) > 1:
        L.append("**By machine**")
        for host, sess, tin, tout, c in machines:
            L.append(f"· **{host}** — ${c:.2f} ({pct_of_week(c)}) · "
                     f"{human(tin)} in / {human(tout)} out · {sess} sessions")
        L.append("")

    # models
    L.append("**By model**")
    for model, i, o, cr, cw, c in by_model(con, where_claude, args):
        L.append(f"· **{model}** — ${c:.2f} ({pct_of_week(c)}) · in {human(i)} · "
                 f"out {human(o)} · cache r/w {human(cr)}/{human(cw)}")
    L.append("")

    # workflows
    L.append("**By workflow**")
    rows = by_workflow(con, where_claude, args)
    multi = len(machines) > 1
    for label, kind, host, sessions, tin, tout, c in rows:
        tag = {"cron": "⏱", "channel": "💬", "terminal": "⌨️", "unattributed": "❓"}.get(kind, "·")
        where_tag = f" _({host})_" if multi and host != "mini" else ""
        L.append(f"{tag} **{label}**{where_tag} — ${c:.2f} ({pct_of_week(c)}) · "
                 f"{human(tin)} in / {human(tout)} out · {sessions} run{'s' if sessions != 1 else ''}")

    # off-machine usage: the one thing that can silently skew every quota share
    om = offmachine.summary(con)
    if om["unseen_models"] or om["fit"]:
        L.append("")
        L.append("**Off-machine usage**")
        for name, pct in om["unseen_models"].items():
            L.append(f"· **{name}** is at {pct:.0f}% of its weekly bucket with "
                     f"zero messages on this Mac — that burn happened elsewhere.")
        f = om["fit"]
        if f and f["offmachine_pct_per_hour"] > 0.05:
            L.append(f"· measured drift not explained by local spend: "
                     f"**{f['offmachine_pct_per_hour']:.2f}% of 5h quota per hour** "
                     f"(n={f['n']} intervals)")
        elif f:
            L.append(f"· no unexplained quota drift detected (n={f['n']} intervals)")
        else:
            L.append(f"· drift regression still accumulating "
                     f"({om['n_intervals']}/{offmachine.MIN_INTERVALS} intervals)")
        if om["unseen_models"]:
            L.append("· ⇒ quota percentages above are **upper bounds**; dollar costs are exact.")

    # Codex runs on a separate ChatGPT plan with its own limits; merging its
    # percentages into Claude's would be meaningless.
    ct = totals(con, where_codex, args)
    if ct["input"] or ct["output"] or ct["cache_read"]:
        cq = con.execute("""SELECT five_hour_pct, seven_day_pct FROM quota_samples
                            WHERE provider='openai' ORDER BY ts DESC LIMIT 1""").fetchone()
        L.append("")
        L.append("__Codex (OpenAI — separate plan, separate limits)__")
        line = (f"· {human(ct['input'] + ct['cache_read'] + ct['output'])} tokens · "
                f"in {human(ct['input'])} fresh + {human(ct['cache_read'])} cached · "
                f"out {human(ct['output'])}")
        if ct["cost"] > 0:
            line += f" · ${ct['cost']:.2f}"
        L.append(line)
        if ct["cost"] == 0:
            L.append("· _not costed — set `token_tracker.openai_prices` in config.json "
                     "to price these_")
        if cq:
            L.append(f"· plan quota at last observation: 5h **{cq[0]:.0f}%** · "
                     f"weekly **{cq[1]:.0f}%**")
        for model, i, o, cr, cw, c in by_model(con, where_codex, args):
            L.append(f"· **{model}** — in {human(i)} · cached {human(cr)} · out {human(o)}")

    # Local models: real tokens, but no dollar cost and no quota, so they get
    # volume only rather than a fabricated place in the cost math.
    lt = totals(con, where_local, args)
    if lt["input"] or lt["output"] or lt["cache_read"]:
        L.append("")
        L.append("__Local models (Ollama / LM Studio — no cost, no quota)__")
        L.append(f"· {human(lt['input'] + lt['cache_read'] + lt['output'])} tokens · "
                 f"in {human(lt['input'] + lt['cache_read'])} · out {human(lt['output'])}")
        for model, i, o, cr, cw, c in by_model(con, where_local, args):
            L.append(f"· **{model}** — in {human(i + cr)} · out {human(o)}")

    # honesty line
    un = con.execute(f"""SELECT COALESCE(SUM(m.cost_usd),0) FROM messages m
                         JOIN attribution a USING(session_id)
                         WHERE {where_claude} AND a.kind='unattributed'""", args).fetchone()[0]
    if t["cost"] > 0 and un > 0:
        L.append("")
        L.append(f"_Unattributed: ${un:.2f} ({100*un/t['cost']:.0f}%) — sessions whose "
                 f"originating workflow could not be identified._")

    return "\n".join(L)


# ---------------------------------------------------------------- delivery

def discord_token():
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        return tok
    if SERVICE_ENV.exists():
        m = re.search(r"DISCORD_BOT_TOKEN='([^']+)'", SERVICE_ENV.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise RuntimeError("DISCORD_BOT_TOKEN not found (env or service-env)")


def post(text, channel=lib.DISCORD_CHANNEL):
    token = discord_token()
    for chunk in chunks(text):
        body = json.dumps({"content": chunk, "flags": 4,
                           "allowed_mentions": {"parse": []}}).encode("utf-8")
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel}/messages",
            data=body,
            headers={"Authorization": f"Bot {token}",
                     "Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "OpenClaw token-tracker"})
        resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
        if not resp.get("id"):
            raise RuntimeError(f"discord post failed: {resp}")


def chunks(text, limit=1900):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--yesterday", action="store_true",
                    help="report on the previous day -- what the 5:25am run wants, "
                         "since 'today' is a few minutes old at that hour")
    ap.add_argument("--week", action="store_true")
    ap.add_argument("--no-post", action="store_true")
    ap.add_argument("--no-collect", action="store_true")
    a = ap.parse_args()

    con = lib.connect()
    lib.init(con)
    if not a.no_collect:
        collect.ingest(con)
        collect.attribute(con)
        try:
            quota_mod.record(con, quota_mod.fetch())
        except Exception as e:                     # report is still useful without a fresh sample
            print(f"warn: quota fetch failed ({e}); using last sample", file=sys.stderr)

    day = a.day
    if a.yesterday and not day:
        day = (datetime.now(PT) - timedelta(days=1)).strftime("%Y-%m-%d")
    text = build(con, day=day, week=a.week)
    print(text)
    if not a.no_post:
        post(text)
        print("\n[posted to discord]", file=sys.stderr)
    con.close()


if __name__ == "__main__":
    main()
