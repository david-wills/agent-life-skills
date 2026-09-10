"""Local HTTP ingest server for Health Auto Export REST POSTs.

Authenticates with a static bearer token (``read_secret("HAE_REST_TOKEN")``)
and persists each POST as:

  - <data_root>/imports/apple-health/raw/hae-<timestamp>.json  (raw archive)
  - <data_root>/knowledge/apple-health/YYYY-MM-DD.md (+ .sidecar/)  (per-day merge)

Endpoints:
  POST /ingest        — accept HAE JSON; bearer auth required
  GET  /healthz       — liveness; no auth
  GET  /status        — last 5 ingests; bearer auth required

Binds 127.0.0.1 by default. If the phone reaches this machine over a tailnet,
bind that interface's IP (``--bind 100.x.y.z``); do not bind 0.0.0.0 on a
machine with any other inbound surface.

Stdlib only, so it runs under launchd with nothing installed.

Usage:
  python3 server.py                                  # 127.0.0.1:8787
  python3 server.py --bind 100.101.102.103 --port 8787
  python3 server.py --data-root /somewhere/else      # override paths.data_root
"""

from __future__ import annotations

import argparse
import os
import hmac
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from read_secret import SecretError, read_secret  # noqa: E402
from parse_hae_payload import ingest_payload  # noqa: E402

DEFAULT_PORT = 8787
DEFAULT_BIND = "127.0.0.1"
MAX_BODY_BYTES = 64 * 1024 * 1024  # 64MB; HAE bursts can be large
TOKEN_SECRET_NAME = "HAE_REST_TOKEN"
RECENT_INGESTS: deque[dict[str, Any]] = deque(maxlen=5)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


class IngestHandler(BaseHTTPRequestHandler):
    server_version = "HAEIngest/1.0"
    expected_token: str = ""
    out_dir: Path = Path(".")
    raw_dir: Path = Path(".")

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
        presented = ""
        if auth.startswith("Bearer "):
            presented = auth.split(" ", 1)[1].strip()
        else:
            # HAE also supports an "API Key" header for some auth modes.
            presented = self.headers.get("X-API-Key", "").strip()
        if not presented:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self.expected_token.encode("utf-8"))

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
            self._send_json(400, {"error": "invalid_json", "detail": f"line {exc.lineno} col {exc.colno}"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "invalid_json", "detail": "expected a JSON object"})
            return
        try:
            report = ingest_payload(payload, out_dir=self.out_dir, raw_archive_dir=self.raw_dir)
        except Exception:
            # Details go to the log, never to the client: a traceback carries
            # absolute paths and whatever the payload contained.
            logging.exception("ingest failed")
            self._send_json(500, {"error": "ingest_failed"})
            return
        summary = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "remote": self.client_address[0],
            "dates": report.get("dates", []),
            "metric_points": report.get("metric_points", 0),
            "workout_count": report.get("workout_count", 0),
        }
        RECENT_INGESTS.append(summary)
        logging.info("ingest ok: %s (raw=%s)", summary, report.get("raw_archive"))
        self._send_json(200, {"ok": True, **{k: v for k, v in summary.items() if k != "remote"}})


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bind", default=DEFAULT_BIND,
                   help=f"Interface to listen on (default {DEFAULT_BIND}; tailnet users bind their tailnet IP).")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT}).")
    p.add_argument("--data-root", type=Path, default=None,
                   help="Override paths.data_root; per-day files go under <data-root>/knowledge/apple-health.")
    return p.parse_args()


def main() -> int:
    os.umask(0o077)  # health data: every file this run creates is owner-only
    args = parse_args()
    _setup_logging()

    if args.data_root is not None:
        root = args.data_root.expanduser()
    else:
        from skill_config import data_root
        root = data_root()
    out_dir = root / "knowledge" / "apple-health"
    raw_dir = root / "imports" / "apple-health" / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    try:
        token = read_secret(TOKEN_SECRET_NAME)
    except SecretError as exc:
        logging.error("cannot resolve %s: %s", TOKEN_SECRET_NAME, exc)
        return 2

    IngestHandler.expected_token = token
    IngestHandler.out_dir = out_dir
    IngestHandler.raw_dir = raw_dir

    server = ThreadingHTTPServer((args.bind, args.port), IngestHandler)
    logging.info("HAE ingest server listening on %s:%d (out=%s)", args.bind, args.port, out_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutdown via SIGINT")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
