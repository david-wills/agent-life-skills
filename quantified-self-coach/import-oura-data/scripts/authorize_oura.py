#!/usr/bin/env python3
"""One-time OAuth bootstrap for Oura API v2 (server-side flow).

Oura killed Personal Access Tokens; v2 is OAuth2-only. The server-side flow
returns BOTH an access token and a refresh token. The importer then auto-refreshes
the access token using the refresh token and persists rotated tokens back.

What this does:
  1. Reads OURA_CLIENT_ID and OURA_CLIENT_SECRET via lib/read_secret.py
     (env var, 1Password, keychain, or secrets.json).
  2. Spins up a local HTTP listener on http://localhost:8080/callback.
  3. Opens a browser to Oura's authorize URL (with state + CSRF guard).
  4. Catches the redirect, exchanges the `code` for access + refresh tokens.
  5. Writes both tokens (plus expires_at) to <data_root>/oura/oura_tokens.json
     (mode 0600). The importer reads/writes this file from then on.

Prereqs (do these first):
  - OAuth2 app created at https://cloud.ouraring.com/oauth/applications
  - Redirect URI in the dev console set EXACTLY to:
        http://localhost:8080/callback
    (case + trailing path sensitive — copy-paste, don't re-type.)
  - Client ID + Client Secret stored where read_secret can find them, as
    OURA_CLIENT_ID and OURA_CLIENT_SECRET.

Usage:
    python3 authorize_oura.py                              # default token file
    python3 authorize_oura.py --output /tmp/oura.json      # custom location

To re-authorize (lost token file, refresh token invalidated, etc.), just re-run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import json
import os
import pathlib
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# Shared helpers live in lib/ at the repo root; walk up so this works from any cwd.
_LIB = next((p / "lib" for p in pathlib.Path(__file__).resolve().parents if (p / "lib" / "skill_config.py").is_file()), None)
if _LIB is None:
    raise SystemExit("cannot find the repo-root lib/ directory; run from a clone of the repo, not a copied file")
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from read_secret import SecretError, read_secret  # noqa: E402


CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8080
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
SCOPE = "personal daily heartrate workout"


_result: dict[str, str | None] = {"code": None, "state": None, "error": None}
_expected_state: str = ""
_done = threading.Event()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib casing)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        _result["code"] = (qs.get("code") or [None])[0]
        _result["state"] = (qs.get("state") or [None])[0]
        _result["error"] = (qs.get("error") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if _result["error"]:
            body = (
                "<html><body><h2>Oura authorization failed</h2>"
                f"<p>Error: <code>{_result['error']}</code></p>"
                "<p>Check the terminal for details.</p></body></html>"
            )
        else:
            body = (
                "<html><body><h2>Oura authorized.</h2>"
                "<p>You can close this tab and return to the terminal.</p></body></html>"
            )
        self.wfile.write(body.encode("utf-8"))
        _done.set()

    def log_message(self, format: str, *args: object) -> None:  # silence stderr spam
        return


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--output",
        default=None,
        help="File path to write the token JSON (mode 0600). Default: <data_root>/oura/oura_tokens.json",
    )
    return p.parse_args()


def _write_tokens(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
    except Exception:
        os.close(fd)
        raise


def main() -> int:
    os.umask(0o077)  # health data: every file this run creates is owner-only
    global _expected_state
    args = _parse_args()
    if args.output:
        output_path = pathlib.Path(args.output).expanduser()
    else:
        from skill_config import data_root
        output_path = data_root() / "oura" / "oura_tokens.json"

    try:
        client_id = read_secret("OURA_CLIENT_ID")
        client_secret = read_secret("OURA_CLIENT_SECRET")
    except SecretError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "Store OURA_CLIENT_ID and OURA_CLIENT_SECRET where lib/read_secret.py "
            "can find them before running this.",
            file=sys.stderr,
        )
        return 1

    if _port_in_use(CALLBACK_HOST, CALLBACK_PORT):
        print(
            f"port {CALLBACK_PORT} is in use — close whatever's listening on it and try again.",
            file=sys.stderr,
        )
        return 1

    _expected_state = secrets.token_urlsafe(16)
    auth_params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "state": _expected_state,
            "redirect_uri": REDIRECT_URI,
        }
    )
    authorize_url = f"{AUTH_URL}?{auth_params}"

    server = http.server.HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print("Opening browser to authorize Oura …")
    print(f"If it doesn't open automatically, paste this into your browser:\n  {authorize_url}\n")
    webbrowser.open(authorize_url)

    _done.wait(timeout=300)
    server.shutdown()

    if not _done.is_set():
        print("timed out after 5 minutes waiting for the callback.", file=sys.stderr)
        return 1
    if _result["error"]:
        print(f"authorization error from Oura: {_result['error']}", file=sys.stderr)
        return 1
    if _result["state"] != _expected_state:
        print(
            f"CSRF state mismatch: expected {_expected_state!r}, got {_result['state']!r}",
            file=sys.stderr,
        )
        return 1
    if not _result["code"]:
        print("no authorization code in callback URL.", file=sys.stderr)
        return 1

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": _result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        print(f"token exchange HTTP error: {exc.code} — {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"token exchange failed: {exc}", file=sys.stderr)
        return 1

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not access_token or not refresh_token:
        print(f"token response missing tokens: {payload}", file=sys.stderr)
        return 1

    expires_at = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(expires_in or 3600))
    ).isoformat().replace("+00:00", "Z")

    tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "token_type": payload.get("token_type", "Bearer"),
        "scope": payload.get("scope", SCOPE),
    }
    _write_tokens(output_path, tokens)

    print()
    print("=" * 60)
    print("Oura authorization successful.")
    print()
    print(f"Tokens written (mode 0600) to:")
    print(f"  {output_path}")
    print()
    print(f"Access token expires at: {expires_at}")
    print(f"Granted scopes: {tokens['scope']}")
    print()
    print("The importer reads/writes this file. No further action needed.")
    print("To rotate (e.g. invalidated refresh token): re-run this script.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
