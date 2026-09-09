"""Shared config, pricing and DB helpers for the token tracker."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2). Walk up to find it
# rather than hardcoding a path: skills are reached through a symlink.
_LIB = next(p / "lib" for p in Path(__file__).resolve().parents
            if (p / "lib" / "skill_config.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from skill_config import cfg  # noqa: E402

# CONVENTIONS.md 1: mutable state lives in <skill>/_state/, never tracked.
# .resolve() matters -- this skill is reached through the workspace symlink, and
# without it _state/ would land beside the symlink instead of in the repo.
SKILL_DIR = Path(__file__).resolve().parent.parent
STATE = SKILL_DIR / "_state"
DB_PATH = str(STATE / "tokens.db")

# The workspace is anchored absolutely, never derived from __file__ (CONVENTIONS.md 4).
WORKSPACE = Path.home() / ".openclaw" / "workspace"
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
OPENCLAW_STATE_DB = os.path.expanduser("~/.openclaw/state/openclaw.sqlite")
OPENCLAW_AGENTS = os.path.expanduser("~/.openclaw/agents")

# Discord channel for all token-tracker output
DISCORD_CHANNEL = str(cfg("discord.channels.token_tracker"))

# Anthropic first-party API list prices, $ per 1M tokens (input, output).
# Cache write 5m = 1.25x input, cache write 1h = 2x input, cache read = 0.1x input.
PRICES = {
    "claude-fable-5-1":  (10.0, 50.0),
    "claude-fable-5":    (10.0, 50.0),
    "claude-opus-5":     (5.0, 25.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    "claude-sonnet-5":   (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}
DEFAULT_PRICE = (5.0, 25.0)  # unknown model -> assume Opus tier, flagged in report

CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0
CACHE_READ_MULT = 0.1

# Claude Fable 5.1 prices cache reads at a flat $0.25/MTok rather than the usual
# 0.1x of input ($1.00/MTok at its $10 input rate) -- pricing it the default way
# would overstate Fable-heavy machines by 4x on their largest input bucket.
CACHE_READ_FLAT = {
    "claude-fable-5-1": 0.25,
}

# Hosts whose transcripts feed this database. 'mini' is where the tracker runs.
LOCAL_HOST = "mini"


# Rollout files record a model id but no provider, and locally-served models
# (Ollama, LM Studio) reach Codex-style harnesses through an OpenAI-compatible
# endpoint -- so qwen3.6 turns up looking like an OpenAI model. Classify by name:
# positively identify the two cloud vendors, treat anything else as local.
OPENAI_PREFIXES = ("gpt", "o1", "o3", "o4", "chatgpt", "codex", "davinci")


def classify_provider(model):
    m = (model or "").lower()
    if "claude" in m:
        return "anthropic"
    if m.startswith(OPENAI_PREFIXES) or "codex" in m:
        return "openai"
    return "local"          # Ollama / LM Studio: no cost, no quota


def openai_prices(model):
    """($/MTok input, output, cached input) for an OpenAI model, or None if unpriced.

    Deliberately config-driven with no baked-in defaults: guessing another
    vendor's list prices would put invented dollar figures in a cost report.
    Set token_tracker.openai_prices in config.json to enable costing.
    """
    table = cfg("token_tracker.openai_prices", {}) or {}
    row = table.get(model) or table.get(model.split("-")[0])
    if not row:
        return None
    inp, out = float(row[0]), float(row[1])
    cached = float(row[2]) if len(row) > 2 else inp * 0.1
    return inp, out, cached


def normalize_model(model: str) -> str:
    """Strip date suffixes and provider prefixes so pricing lookups hit."""
    if not model:
        return "unknown"
    m = model.strip()
    if "/" in m:
        m = m.split("/")[-1]
    if m in PRICES:
        return m
    # trim a trailing -YYYYMMDD snapshot suffix
    parts = m.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8 and parts[0] in PRICES:
        return parts[0]
    return m


def cost_usd(model, inp, out, cache_read, cw5m, cw1h):
    """API-equivalent cost in USD for one usage record."""
    m = normalize_model(model)
    pin, pout = PRICES.get(m, DEFAULT_PRICE)
    read_rate = CACHE_READ_FLAT.get(m, pin * CACHE_READ_MULT)
    return (
        inp * pin
        + out * pout
        + cache_read * read_rate
        + cw5m * pin * CACHE_WRITE_5M_MULT
        + cw1h * pin * CACHE_WRITE_1H_MULT
    ) / 1_000_000.0


def oauth_token() -> str:
    """Read the live Claude Code OAuth access token from the macOS keychain."""
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True, timeout=20,
    ).stdout
    return json.loads(raw)["claudeAiOauth"]["accessToken"]


def connect(path=DB_PATH):
    parent = os.path.dirname(path)
    if parent:                      # ":memory:" and bare filenames have none
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  uuid            TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  project         TEXT,
  ts              INTEGER NOT NULL,          -- epoch ms, UTC
  day             TEXT NOT NULL,             -- YYYY-MM-DD, America/Los_Angeles
  model           TEXT,
  entrypoint      TEXT,                      -- 'sdk-cli' (OpenClaw) | 'cli' (terminal)
  is_sidechain    INTEGER DEFAULT 0,
  input_tokens    INTEGER DEFAULT 0,
  output_tokens   INTEGER DEFAULT 0,
  cache_read      INTEGER DEFAULT 0,
  cache_write_5m  INTEGER DEFAULT 0,
  cache_write_1h  INTEGER DEFAULT 0,
  thinking_tokens INTEGER DEFAULT 0,
  cost_usd        REAL DEFAULT 0,
  host            TEXT NOT NULL DEFAULT 'mini',
  provider        TEXT NOT NULL DEFAULT 'anthropic',
  request_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_day    ON messages(day);
CREATE INDEX IF NOT EXISTS idx_messages_ts     ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_sess   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_host   ON messages(host, day);

-- incremental ingest bookkeeping
CREATE TABLE IF NOT EXISTS files (
  path       TEXT PRIMARY KEY,
  size       INTEGER NOT NULL,
  mtime      REAL NOT NULL,
  offset     INTEGER NOT NULL
);

-- one row per claude-cli session, mapping it to the workflow that caused it
CREATE TABLE IF NOT EXISTS attribution (
  session_id  TEXT PRIMARY KEY,
  host        TEXT DEFAULT 'mini',
  kind        TEXT,      -- cron | channel | terminal | unattributed
  label       TEXT,      -- human name: "newsfeed", "discord:#coach", "terminal:workspace"
  agent       TEXT,      -- agent id, e.g. main | work
  job_id      TEXT,
  method      TEXT,      -- how we matched: cron-window | sessions.json | entrypoint
  confidence  TEXT       -- high | medium | low
);

-- one row per alert already sent, so a condition is announced once
CREATE TABLE IF NOT EXISTS alerts (
  kind    TEXT NOT NULL,
  ref_ts  INTEGER NOT NULL,
  sent_at INTEGER NOT NULL,
  PRIMARY KEY (kind, ref_ts)
);

-- periodic snapshots of real subscription quota utilization
CREATE TABLE IF NOT EXISTS quota_samples (
  ts               INTEGER PRIMARY KEY,   -- epoch ms
  five_hour_pct    REAL,
  five_hour_reset  TEXT,
  seven_day_pct    REAL,
  seven_day_reset  TEXT,
  opus_pct         REAL,
  raw_json         TEXT,
  scoped_json      TEXT,                  -- per-model weekly buckets (e.g. Fable)
  provider         TEXT DEFAULT 'anthropic'
);
"""


def init(con):
    # Migrate first: SCHEMA creates an index over messages(host, ...), which fails
    # on a pre-multi-host database if the column isn't added before the script runs.
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, col, decl in (("messages", "host", "TEXT NOT NULL DEFAULT 'mini'"),
                             ("messages", "provider", "TEXT NOT NULL DEFAULT 'anthropic'"),
                             ("messages", "request_id", "TEXT"),
                             ("attribution", "host", "TEXT DEFAULT 'mini'"),
                             ("quota_samples", "provider", "TEXT DEFAULT 'anthropic'")):
        if table not in existing:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.executescript(SCHEMA)
    con.commit()
