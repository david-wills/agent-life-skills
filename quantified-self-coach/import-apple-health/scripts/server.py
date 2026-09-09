"""Local HTTP ingest server for Health Auto Export REST POSTs.

Listens on the Tailscale interface, authenticates via a static bearer token
(see read_secret HAE_REST_TOKEN), and persists each POST as:

  - `imports/apple-health/raw/hae-<timestamp>.json` (full payload archive)
  - `knowledge/apple-health/YYYY-MM-DD.md` (per-day merged markdown)

Endpoints:
  POST /ingest        — accept HAE JSON; bearer auth required
  GET  /healthz       — liveness; no auth
  GET  /status        — last 5 ingests; bearer auth required

Configuration knobs (all overridable via env):
  HAE_SERVER_PORT     — default 8787
  HAE_SERVER_HOST     — default 0.0.0.0 (bind all; firewall via Tailscale ACL)
  HAE_OUT_DIR         — default <workspace>/knowledge/apple-health
  HAE_RAW_DIR         — default <workspace>/imports/apple-health/raw
  HAE_LOG_PATH        — default <workspace>/imports/apple-health/server.log
  HAE_REST_TOKEN      — bearer token (preferred via 1P/keychain)

The server is intentionally stdlib-only (no FastAPI/uvicorn) so it has zero
install-time dependencies and runs out of the box under launchd.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
# The workspace is a fixed location, not derivable from this file's path: skills
# live in ~/Local/skills and are reached through a symlink, so .resolve() lands in
# the repo, not the workspace (this broke the email digest on 2026-09-03).
WORKSPACE = Path.home() / ".openclaw" / "workspace"

# Shared helpers live in the repo-root lib/ (CONVENTIONS.md 2). Walk up to find it
# rather than hardcoding a path: skills are reached through a symlink.
from pathlib import Path as _P  # noqa: E402
_LIB = next(p / "lib" for p in _P(__file__).resolve().parents if (p / "lib" / "read_secret.py").is_file())
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
sys.path.insert(0, str(SCRIPT_DIR))

from read_secret import read_secret, SecretError  # noqa: E402
from parse_hae_payload import ingest_payload  # noqa: E402

DEFAULT_PORT = 8787
DEFAULT_HOST = "0.0.0.0"
DEFAULT_OUT_DIR = WORKSPACE / "knowledge" / "apple-health"
DEFAULT_RAW_DIR = WORKSPACE / "imports" / "apple-health" / "raw"
DEFAULT_LOG_PATH = WORKSPACE / "imports" / "apple-health" / "server.log"
MAX_BODY_BYTES = 64 * 1024 * 1024  # 64MB; HAE bursts can be large
TOKEN_SECRET_NAME = "HAE_REST_TOKEN"
RECENT_INGESTS: deque[dict[str, Any]] = deque(maxlen=5)


def _resolve_token() -> str:
    return read_secret(TOKEN_SECRET_NAME)


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


class IngestHandler(BaseHTTPRequestHandler):
    server_version = "OpenClawHAE/1.0"
    expected_token: str = ""
    out_dir: Path = DEFAULT_OUT_DIR
    raw_dir: Path = DEFAULT_RAW_DIR

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bearer_ok(self) -> bool:
        if not self.expected_token:
            return False
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth.split(" ", 1)[1].strip() == self.expected_token
        # HAE also supports an "API Key" header for some auth modes.
        api_key = self.headers.get("X-API-Key", "")
        if api_key:
            return api_key.strip() == self.expected_token
        return False

    def do_GET(self) -> None:  # noqa: N802 (required signature)
        if self.path == "/healthz":
            self._send_json(200, {"ok": True, "ts": time.time()})
            return
        if self.path == "/status":
            if not self._bearer_ok():
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, {"recent_ingests": list(RECENT_INGESTS)})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 (required signature)
        if self.path != "/ingest":
            self._send_json(404, {"error": "not_found"})
            return
        if not self._bearer_ok():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "bad_content_length"})
            return
        if length <= 0:
            self._send_json(400, {"error": "empty_body"})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "payload_too_large", "max_bytes": MAX_BODY_BYTES})
            return
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": "invalid_json", "detail": str(exc)})
            return
        try:
            report = ingest_payload(payload, out_dir=self.out_dir, raw_archive_dir=self.raw_dir)
        except Exception as exc:
            logging.exception("ingest failed")
            self._send_json(500, {"error": "ingest_failed", "detail": str(exc), "trace": traceback.format_exc()[-800:]})
            return
        report_summary = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "remote": self.client_address[0],
            "dates": report.get("dates", []),
            "metric_points": report.get("metric_points", 0),
            "workout_count": report.get("workout_count", 0),
            "raw_archive": report.get("raw_archive"),
        }
        RECENT_INGESTS.append(report_summary)
        logging.info("ingest ok: %s", report_summary)
        self._send_json(200, {"ok": True, "report": report})


def main() -> int:
    port = int(os.environ.get("HAE_SERVER_PORT", DEFAULT_PORT))
    host = os.environ.get("HAE_SERVER_HOST", DEFAULT_HOST)
    out_dir = Path(os.environ.get("HAE_OUT_DIR", DEFAULT_OUT_DIR))
    raw_dir = Path(os.environ.get("HAE_RAW_DIR", DEFAULT_RAW_DIR))
    log_path = Path(os.environ.get("HAE_LOG_PATH", DEFAULT_LOG_PATH))
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    _setup_logging(log_path)

    try:
        token = _resolve_token()
    except SecretError as exc:
        logging.error("cannot resolve %s: %s", TOKEN_SECRET_NAME, exc)
        return 2

    IngestHandler.expected_token = token
    IngestHandler.out_dir = out_dir
    IngestHandler.raw_dir = raw_dir

    server = ThreadingHTTPServer((host, port), IngestHandler)
    logging.info("HAE ingest server listening on %s:%d (out=%s)", host, port, out_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutdown via SIGINT")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
