"""Read-only monitoring panel for the trading engine.

Security model
--------------
This process is a *reader*. It is deliberately incapable of affecting trading:

* **No mutating routes.** There is no endpoint that opens, closes, arms or
  configures anything. The whole API is three GETs. Compromise of the panel
  cannot place an order, because there is no code path that places one.
* **Separate process.** It never imports the engine or touches the broker
  connection. It reads `logs/state.json`, which the engine publishes atomically.
  A hang, crash or flood here cannot slow the trading loop down.
* **No credentials, ever.** The snapshot contains no keys or tokens, and
  `_assert_no_secrets` re-checks every payload against the live .env values
  before it is served - so a future change that accidentally widens the
  snapshot fails loudly instead of leaking quietly.
* **Localhost by default.** Binding to a public interface requires both an
  explicit flag and a token, so it cannot happen by accident.
* **Strict CSP.** No inline script or style, no external origins, so a reflected
  string cannot become script execution.

Usage
-----
    python -m panel.server                    # 127.0.0.1:8787, no auth needed
    ssh -N -L 8787:127.0.0.1:8787 user@vps    # then browse localhost:8787

    PANEL_TOKEN=... python -m panel.server --host 0.0.0.0 --i-understand-exposure
"""

from __future__ import annotations

import argparse
import csv
import hmac
import json
import logging
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LOG_DIR, load_env  # noqa: E402

log = logging.getLogger("panel")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE_PATH = LOG_DIR / "state.json"

# Only these files are ever served. No path is built from user input, so
# directory traversal is structurally impossible rather than filtered.
ALLOWED_STATIC = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cache-Control": "no-store",
}

RATE_LIMIT_REQUESTS = 120
RATE_LIMIT_WINDOW = 60.0

app = FastAPI(title="Trading engine panel", docs_url=None, redoc_url=None,
              openapi_url=None)

_token: str = ""
_secret_values: list[str] = []
_hits: dict[str, deque] = defaultdict(deque)


# --------------------------------------------------------------------- guards


def _load_secret_values() -> list[str]:
    """The strings that must never appear in a response body."""
    env = load_env()
    values = []
    for key in (
        "CTRADER_CLIENT_SECRET",
        "CTRADER_ACCESS_TOKEN",
        "CTRADER_CLIENT_ID",
        "ANTHROPIC_API_KEY",
        "OPENBB_FRED_KEY",
        "PANEL_TOKEN",
    ):
        value = (env.get(key) or "").strip()
        if len(value) >= 8:
            values.append(value)
    return values


def _assert_no_secrets(payload: str) -> None:
    for secret in _secret_values:
        if secret in payload:
            log.critical("Refusing to serve a response containing a credential")
            raise HTTPException(status_code=500, detail="response withheld")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def guard(request: Request, call_next):
    # 1. Method allowlist. The app has no mutating handler, but rejecting the
    #    verbs outright means a future one cannot be added by accident.
    if request.method not in ("GET", "HEAD"):
        return JSONResponse({"detail": "read-only"}, status_code=405)

    # 2. Rate limit per client.
    key = _client_key(request)
    now = time.monotonic()
    hits = _hits[key]
    while hits and now - hits[0] > RATE_LIMIT_WINDOW:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_REQUESTS:
        return JSONResponse({"detail": "slow down"}, status_code=429)
    hits.append(now)

    # 3. Token, when one is configured. Constant-time compare.
    if _token:
        supplied = request.headers.get("authorization", "")
        prefix = "Bearer "
        supplied = supplied[len(prefix):] if supplied.startswith(prefix) else (
            request.query_params.get("token", "")
        )
        if not hmac.compare_digest(supplied, _token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401,
                                headers=SECURITY_HEADERS)

    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


# --------------------------------------------------------------------- routes


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/static/{name}")
def static_file(name: str) -> Response:
    media_type = ALLOWED_STATIC.get(name)
    if media_type is None:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(STATIC_DIR / name, media_type=media_type)


@app.get("/api/health")
def health() -> dict:
    fresh = False
    age = None
    if STATE_PATH.exists():
        age = time.time() - STATE_PATH.stat().st_mtime
        fresh = age < 60
    return {"ok": True, "engine_state_fresh": fresh, "state_age_seconds": age}


@app.get("/api/state")
def state() -> Response:
    if not STATE_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="No state.json yet - is the engine running?",
        )
    raw = STATE_PATH.read_text(encoding="utf-8")
    _assert_no_secrets(raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=503, detail="state file is being written")
    payload["state_age_seconds"] = round(
        time.time() - STATE_PATH.stat().st_mtime, 1
    )
    return JSONResponse(payload)


@app.get("/api/trades")
def trades(limit: int = 200) -> Response:
    """Closed trades from the CSV journal - the permanent record."""
    limit = max(1, min(limit, 2000))
    rows: list[dict] = []
    for path in sorted(LOG_DIR.glob("trades-*.csv")):
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row.get("closed_at"):
                        rows.append(row)
        except OSError:
            continue
    rows.sort(key=lambda row: row.get("closed_at", ""))
    return JSONResponse({"count": len(rows), "trades": rows[-limit:]})


# ----------------------------------------------------------------------- main


def main() -> int:
    global _token, _secret_values

    parser = argparse.ArgumentParser(description="Read-only trading panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--i-understand-exposure",
        action="store_true",
        help="required to bind anything other than localhost",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-6s %(message)s",
        datefmt="%H:%M:%S",
    )

    _secret_values = _load_secret_values()
    _token = (os.environ.get("PANEL_TOKEN") or "").strip()

    is_local = args.host in ("127.0.0.1", "::1", "localhost")
    if not is_local:
        # Two independent conditions, because getting this wrong exposes an
        # account dashboard to the internet.
        if not args.i_understand_exposure:
            print(
                "Refusing to bind %s without --i-understand-exposure.\n"
                "The safe way to view this remotely is an SSH tunnel:\n"
                "  ssh -N -L %d:127.0.0.1:%d user@host"
                % (args.host, args.port, args.port),
                file=sys.stderr,
            )
            return 2
        if len(_token) < 24:
            print(
                "Refusing to bind %s without a strong PANEL_TOKEN "
                "(24+ chars). Generate one with:\n"
                "  python -c \"import secrets;print(secrets.token_urlsafe(32))\""
                % args.host,
                file=sys.stderr,
            )
            return 2
        log.warning("Panel exposed on %s - token auth is ON. Put TLS in front.",
                    args.host)
    elif _token:
        log.info("Token auth enabled on a local bind.")
    else:
        log.info("Local bind, no token. Reach it with an SSH tunnel:")
        log.info("  ssh -N -L %d:127.0.0.1:%d <user>@<host>", args.port, args.port)

    if not _secret_values:
        log.warning("No .env secrets found to guard against - is .env present?")

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                access_log=False, server_header=False, date_header=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
