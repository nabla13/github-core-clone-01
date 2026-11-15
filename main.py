"""forge — a standalone git forge in one process (make.fun github-core-clone-01).

Lifecycle (sealed trial):
  1. Read MAKEFUN_ENV_URL and MAKEFUN_SHOP_TOKEN from the environment.
  2. Bind an ephemeral port on 0.0.0.0 (never 8080) and start serving.
  3. POST $MAKEFUN_ENV_URL/register {"port": N} (200 wins; 409 = another
     instance already registered -> exit 1; 400/405 = fatal; other failures
     retried with backoff 250 ms -> 2000 ms).
  4. Serve until GET $MAKEFUN_ENV_URL/done returns {"done": true}, then
     exit 0.

Local practice: FORGE_DEV=1 skips the register/done handshake and serves on
PORT (default 8080) forever. MAKEFUN_SHOP_TOKEN, when set, is the only Bearer
token accepted on REST/GraphQL/MCP; git endpoints are never authenticated.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge import graphql as graphql_mod
from forge import githttp as githttp_mod
from forge import httpd
from forge import mcp as mcp_mod
from forge import rest as rest_mod
from forge.scenario import build_scenario
from forge.store import Store


def _log(message: str) -> None:
    print(f"[forge] {message}", file=sys.stderr, flush=True)


def _http_json(method: str, url: str, payload=None, timeout: float = 5.0):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            parsed = {}
        return err.code, parsed
    except Exception as err:  # connection refused, timeout, DNS, ...
        return None, {"error": str(err)}


def _register(env_url: str, port: int) -> None:
    url = env_url.rstrip("/") + "/register"
    delay = 0.25
    while True:
        status, _body = _http_json("POST", url, {"port": port})
        if status == 200:
            _log(f"registered on port {port}")
            return
        if status == 409:
            _log("register: another instance already registered (409); exiting")
            sys.exit(1)
        if status in (400, 405):
            _log(f"register: fatal HTTP {status}; exiting")
            sys.exit(1)
        _log(f"register: HTTP {status}; retrying in {delay:.2f}s")
        time.sleep(delay)
        delay = min(delay * 2, 2.0)


def _wait_done(env_url: str, stop: threading.Event) -> None:
    url = env_url.rstrip("/") + "/done"
    while not stop.is_set():
        status, body = _http_json("GET", url, timeout=5.0)
        if status == 200 and isinstance(body, dict) and body.get("done") is True:
            _log("trial done; exiting 0")
            return
        stop.wait(0.5)


def build_server(token: str | None) -> httpd.ForgeHTTPServer:
    # The bearer token names the scenario viewer (adaiversen, id 1000).
    store = Store(build_scenario()["viewer"]["login"])
    router = httpd.Router()
    rest_mod.register(router, token)
    graphql_mod.register(router, token)
    mcp_mod.register(router, token)
    githttp_mod.register(router)

    def healthz(request):
        return httpd.json_response(request.store, 200, {"ok": True})

    router.add("GET", "/healthz", healthz)

    return httpd.ForgeHTTPServer(
        ("0.0.0.0", 0), httpd.make_handler(store, router, token),
        bind_and_activate=False)


def main() -> int:
    env_url = os.environ.get("MAKEFUN_ENV_URL")
    token = os.environ.get("MAKEFUN_SHOP_TOKEN") or None
    dev = os.environ.get("FORGE_DEV") == "1"

    if not env_url and not dev:
        _log("MAKEFUN_ENV_URL is not set; exiting 1")
        return 1

    server = build_server(token)

    if dev:
        port = int(os.environ.get("PORT", "8080"))
        server.server_address = ("0.0.0.0", port)
        server.server_bind()
        server.server_activate()
        _log(f"dev mode: listening on 0.0.0.0:{port} "
             f"(token {'set' if token else 'unset'})")
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
        return 0

    server.server_bind()
    server.server_activate()
    port = server.server_address[1]
    _log(f"listening on 0.0.0.0:{port}")

    stop = threading.Event()

    def _on_signal(signum, _frame):
        _log(f"signal {signum}; shutting down")
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    serve_thread = threading.Thread(target=server.serve_forever,
                                    kwargs={"poll_interval": 0.1}, daemon=True)
    serve_thread.start()
    try:
        _register(env_url, port)
        _wait_done(env_url, stop)
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
