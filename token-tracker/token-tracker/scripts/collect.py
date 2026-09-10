#!/usr/bin/env python3
"""Ingest Claude Code transcripts into tokens.db and attribute each session to a workflow.

Ground truth for tokens is ~/.claude/projects/**/*.jsonl -- every assistant message
carries a full usage breakdown (input / output / cache read / cache write 5m+1h /
thinking). Everything OpenClaw runs through the claude-cli provider lands there too,
tagged entrypoint=sdk-cli; the user's own terminal sessions are entrypoint=cli.

Attribution joins those sessions back to the workflow that caused them:
  1. cron   -- openclaw.sqlite cron_run_logs, matched by run time window + model
  2. channel-- agents/*/sessions/sessions.json claudeCliSessionId -> session key
  3. terminal -- entrypoint=cli

Steps 1-2 read the OpenClaw gateway's own state and are skipped when it is not
installed; every session then resolves to terminal:<project> or unattributed.

Usage:
  collect.py                  # incremental ingest + attribute new sessions
  collect.py --reattribute    # drop every label and rebuild them (after rule changes)
  collect.py --quiet          # no progress lines (what sample.py wants)
"""
import argparse
import glob
import json
import re
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib



def iso_to_ms(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(lib.day_tz()).strftime("%Y-%m-%d")


def ingest(con, verbose=False, root=None, host=None):
    """Incrementally read every transcript, inserting assistant messages with usage."""
    root = root or lib.CLAUDE_PROJECTS
    host = host or lib.local_host()
    seen = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT path,size,mtime,offset FROM files")}
    new_rows = 0
    files_touched = 0

    # Recursive: subagent transcripts live a level deeper, at
    # <project>/<session>/subagents/agent-*.jsonl, and carry real billable usage.
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = seen.get(path)
        offset = 0
        if prev:
            psize, pmtime, poff = prev
            if st.st_size == psize and st.st_mtime == pmtime:
                continue                      # unchanged
            offset = poff if st.st_size >= psize else 0   # truncated/rewritten -> full reread

        project = path.rsplit("/projects/", 1)[1].split("/")[0]
        session_id = os.path.basename(path).split(".")[0]
        rows = []
        # A single API response is written as one record per content block, each
        # repeating the SAME usage object. Counting records instead of calls
        # inflated every figure ~2.4x. requestId identifies the actual call.
        # Resuming mid-file, seed from what is already stored so a call whose
        # first block landed on the previous pass is not counted again.
        seen_requests = set()
        if offset:
            seen_requests = {r[0] for r in con.execute(
                "SELECT request_id FROM messages WHERE session_id=? AND request_id IS NOT NULL",
                (session_id,))}
        new_offset = offset
        try:
            # Binary mode so byte offsets are exact. A live transcript usually ends
            # in a half-written line: stop at the START of any line that has no
            # newline yet, so the next pass re-reads it whole. Storing EOF here
            # would skip that line forever once it completed.
            with open(path, "rb") as fh:
                fh.seek(offset)
                while True:
                    raw = fh.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        break                 # partial trailing line; offset stays before it
                    new_offset += len(raw)
                    if b'"usage"' not in raw:
                        continue
                    try:
                        d = json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue              # a complete but corrupt line; skip it
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage") or {}
                    if not u:
                        continue
                    model = msg.get("model") or ""
                    if not model or model.startswith("<"):
                        continue              # synthetic / non-billed
                    ts = iso_to_ms(d.get("timestamp"))
                    if ts is None:
                        continue
                    req = d.get("requestId") or msg.get("id")
                    if req:
                        if req in seen_requests:
                            continue          # same API call, another content block
                        seen_requests.add(req)
                    cc = u.get("cache_creation") or {}
                    cw5 = cc.get("ephemeral_5m_input_tokens", 0)
                    cw1 = cc.get("ephemeral_1h_input_tokens", 0)
                    if not cc:                # older transcripts: only the aggregate field
                        cw5 = u.get("cache_creation_input_tokens", 0)
                    inp = u.get("input_tokens", 0)
                    out = u.get("output_tokens", 0)
                    cr = u.get("cache_read_input_tokens", 0)
                    think = (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
                    rows.append((
                        d.get("uuid"), d.get("sessionId") or session_id, project, ts, day_of(ts),
                        lib.normalize_model(model), d.get("entrypoint"),
                        1 if d.get("isSidechain") else 0,
                        inp, out, cr, cw5, cw1, think,
                        lib.cost_usd(model, inp, out, cr, cw5, cw1), host, req,
                    ))
        except OSError:
            continue

        if rows:
            before = con.total_changes
            con.executemany(
                """INSERT OR IGNORE INTO messages
                   (uuid, session_id, project, ts, day, model, entrypoint, is_sidechain,
                    input_tokens, output_tokens, cache_read, cache_write_5m,
                    cache_write_1h, thinking_tokens, cost_usd, host, request_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            new_rows += con.total_changes - before
        con.execute("INSERT OR REPLACE INTO files (path, size, mtime, offset) VALUES (?,?,?,?)",
                    (path, st.st_size, st.st_mtime, new_offset))
        files_touched += 1

    con.commit()
    if verbose:
        print(f"ingest[{host}]: {files_touched} files touched, {new_rows} new message rows")
    return files_touched


def load_cron_runs():
    """Every cron run OpenClaw has logged, with its job name. Empty without OpenClaw."""
    db = lib.openclaw_paths()["state_db"]
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute("""
            SELECT r.job_id, COALESCE(j.name, r.job_id), r.run_at_ms, r.ts, r.model, r.session_key
            FROM cron_run_logs r
            LEFT JOIN cron_jobs j ON j.job_id = r.job_id
            WHERE r.run_at_ms IS NOT NULL
        """).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for job_id, name, run_at, finish, model, skey in rows:
        agent = skey.split(":")[1] if skey and skey.startswith("agent:") else "main"
        out.append(dict(job_id=job_id, name=name, start=run_at,
                        end=max(finish or run_at, run_at), model=lib.normalize_model(model or ""),
                        agent=agent))
    out.sort(key=lambda r: r["start"])
    return out


def load_channel_map():
    """claude-cli session id -> (agent, human label) from every agent's sessions.json.

    Empty when OpenClaw is not installed; glob on a missing directory yields nothing.
    """
    out = {}
    for sj in glob.glob(os.path.join(lib.openclaw_paths()["agents"], "*", "sessions", "sessions.json")):
        agent = sj.split("/agents/")[1].split("/")[0]
        try:
            data = json.load(open(sj))
        except (OSError, json.JSONDecodeError):
            continue
        for key, e in data.items():
            if not isinstance(e, dict):
                continue
            cid = e.get("claudeCliSessionId") or (e.get("cliSessionIds") or {}).get("claude-cli")
            if not cid:
                continue
            label = e.get("displayName") or e.get("groupChannel") or ":".join(key.split(":")[2:4])
            out[cid] = (agent, prettify(label))
    return out


CHAT_ID_RE = re.compile(r'"chat_id":\s*"([^"]+)"')


def _text_of(d):
    c = d.get("content")
    if isinstance(c, str):
        return c
    mc = (d.get("message") or {}).get("content")
    if isinstance(mc, str):
        return mc
    if isinstance(mc, list):
        return "\n".join(b.get("text", "") for b in mc if isinstance(b, dict))
    return ""


def scan_chat_id(session_id):
    """Pull the inbound chat_id out of a transcript's opening context block.

    This is the durable attribution signal for interactive sessions: it lives in
    the transcript itself, so it still works long after sessions.json has rebound
    that channel to a newer claude-cli session.
    """
    g = glob.glob(os.path.join(lib.CLAUDE_PROJECTS, "*", f"{session_id}.jsonl"))
    if not g:
        return None
    try:
        with open(g[0], errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 60:
                    break
                if "chat_id" not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = CHAT_ID_RE.search(_text_of(d))
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def short_project(project):
    """'-Users-you--openclaw-workspace-work' -> 'workspace/work'."""
    if not project:
        return "unknown"
    # Claude Code names a project dir after its path with '/' -> '-', so the
    # home directory arrives as a prefix like '-Users-you-'. Strip it.
    home = os.path.expanduser("~").replace("/", "-")
    p = project.replace(home + "-", "", 1)
    # The gateway's workspace hides under a dot directory, so it arrives as
    # '-openclaw-workspace'. Collapse that before trimming the leading dash.
    p = p.replace("-openclaw-workspace", "workspace").lstrip("-")
    return p.replace("workspace-", "workspace/")


def prettify(label):
    """Collapse the several shapes OpenClaw uses into one stable channel name.

    'discord:<guild>#coach', 'discord:g-<guild>' and 'discord:direct' all describe
    places; without this they'd show up as separate rows for the same channel.
    """
    if not label:
        return label
    if label.endswith(":direct") or label == "direct":
        return f"{label.split(':')[0]}:DM" if ":" in label else "DM"
    if "#" in label:
        head, tail = label.split("#", 1)
        return f"{head.split(':')[0]}:#{tail}"
    return label


def load_gid_names():
    """Discord/Slack group id -> friendly '#channel' name, from every sessions.json."""
    names = {}
    for sj in glob.glob(os.path.join(lib.openclaw_paths()["agents"], "*", "sessions", "sessions.json")):
        try:
            data = json.load(open(sj))
        except (OSError, json.JSONDecodeError):
            continue
        for e in data.values():
            if not isinstance(e, dict):
                continue
            gid, disp = e.get("groupId"), e.get("displayName")
            if not gid or not disp:
                continue
            pretty = prettify(disp)
            # a name carrying a real #channel beats a bare guild fallback
            if gid not in names or ("#" in pretty and "#" not in names[gid]):
                names[gid] = pretty
    return names


def label_chat_id(chat_id, gid_names):
    if not chat_id:
        return None
    if chat_id.startswith("user:"):
        return "discord:DM"   # OpenClaw's only DM peer is the user
    if ":" in chat_id:
        gid = chat_id.split(":", 1)[1]
        return gid_names.get(gid, f"channel:{gid}")
    return gid_names.get(chat_id, chat_id)


def attribute(con, verbose=False, reset=False):
    """Label every claude-cli session with the workflow that produced it."""
    if reset:
        con.execute("DELETE FROM attribution")
    sessions = con.execute("""
        SELECT m.session_id, MIN(m.ts), MAX(m.ts), MIN(m.entrypoint), m.project, m.host
        FROM messages m
        LEFT JOIN attribution a ON a.session_id = m.session_id
        WHERE a.session_id IS NULL OR a.confidence != 'high'
        GROUP BY m.session_id
    """).fetchall()
    crons = load_cron_runs()
    chan = load_channel_map()
    gid_names = load_gid_names()
    local = lib.local_host()

    # bucket cron runs by minute so lookup stays linear
    starts = [c["start"] for c in crons]
    import bisect

    rows = []
    for sid, first, last, entry, project, host in sessions:
        models = {r[0] for r in con.execute(
            "SELECT DISTINCT model FROM messages WHERE session_id=?", (sid,))}

        # OpenClaw only runs on the local machine, so cron and channel joins are
        # meaningless for rows imported from another host -- a remote session that
        # merely overlaps a local cron window would otherwise be labelled as that cron.
        remote = host != local

        # 1) cron: session must start inside a run window AND finish by the time
        #    that run finished (isolated cron sessions satisfy both; a long-lived
        #    main session that merely overlaps a cron does not).
        best = None
        i = 0 if remote else bisect.bisect_right(starts, first + 15_000)
        for c in crons[max(0, i - 40):i]:
            if not (c["start"] - 15_000 <= first <= c["end"] + 30_000):
                continue
            if last > c["end"] + 60_000:
                continue
            if c["model"] and models and c["model"] not in models:
                continue
            d = abs(first - c["start"])
            if best is None or d < best[0]:
                best = (d, c)
        if best:
            c = best[1]
            rows.append((sid, host, "cron", c["name"], c["agent"], c["job_id"],
                         "cron-window", "high"))
            continue

        # 2) interactive OpenClaw session bound to a channel
        if not remote and sid in chan:
            agent, label = chan[sid]
            rows.append((sid, host, "channel", label, agent, None, "sessions.json", "high"))
            continue

        # 3) interactive session identified from the transcript's own context block
        label = None if remote else label_chat_id(scan_chat_id(sid), gid_names)
        if label:
            rows.append((sid, host, "channel", label, None, None, "chat_id", "high"))
            continue

        # 4) the user driving Claude Code directly. With no OpenClaw state this is
        #    where every interactive session lands.
        if entry == "cli" or remote:
            rows.append((sid, host, "terminal", f"terminal:{short_project(project)}",
                         None, None, "entrypoint", "high"))
            continue

        rows.append((sid, host, "unattributed", f"unknown:{short_project(project)}",
                     None, None, "fallback", "low"))

    con.executemany("""INSERT OR REPLACE INTO attribution
                       (session_id, host, kind, label, agent, job_id, method, confidence)
                       VALUES (?,?,?,?,?,?,?,?)""", rows)
    con.commit()
    if verbose:
        from collections import Counter
        print("attribution:", dict(Counter(r[2] for r in rows)))
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--reattribute", action="store_true",
                    help="delete every attribution row and relabel all sessions")
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    a = ap.parse_args()
    verbose = not a.quiet
    con = lib.connect()
    lib.init(con)
    ingest(con, verbose)
    attribute(con, verbose, reset=a.reattribute)
    if verbose:
        n, lo, hi = con.execute(
            "SELECT COUNT(*), MIN(day), MAX(day) FROM messages").fetchone()
        print(f"db: {n} messages, {lo} .. {hi}")
    con.close()


if __name__ == "__main__":
    main()
