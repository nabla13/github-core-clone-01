"""HTTP server core: ThreadingHTTPServer, HTTP/1.1 keep-alive, routing.

Every reply is JSON with Content-Length and a non-empty X-GitHub-Request-Id
(a deterministic counter). Unknown routes answer JSON 404, never HTML.
"""
from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs


class ForgeHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with TCP_NODELAY on every accepted socket.

    Without it, small responses on a keep-alive connection stall ~40 ms each
    (Nagle + the peer's delayed ACK) — fatal for the sequential read/write
    benchmark workloads, which need ~1-2 ms per request.
    """
    daemon_threads = True
    # 50 concurrent clones arrive as one burst; the default backlog of 5
    # resets most of them. 128 lets the burst queue instead.
    request_queue_size = 128

    def get_request(self):
        sock, addr = super().get_request()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock, addr

from .store import ForgeError


class Request:
    __slots__ = ("method", "path", "query", "headers", "body", "host", "store")

    def __init__(self, method, path, query, headers, body, host, store):
        self.method = method
        self.path = path
        self.query = query          # dict[str, list[str]]
        self.headers = headers      # case-insensitive-ish dict (lower-cased keys)
        self.body = body            # bytes
        self.host = host            # Host header value (for absolute URLs)
        self.store = store

    def query_one(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    def json(self):
        """Parse the body as JSON. Raises ForgeError(400) on malformed JSON."""
        if not self.body:
            return {}
        try:
            value = json.loads(self.body.decode("utf-8"))
        except Exception:
            raise ForgeError(400, "Malformed JSON body")
        if not isinstance(value, dict):
            raise ForgeError(400, "JSON body must be an object")
        return value

    def json_any(self):
        """Parse the body as any JSON value (for GraphQL batching)."""
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            raise ForgeError(400, "Malformed JSON body")


class Response:
    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body: bytes = b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}


def json_response(store, status: int, value, extra_headers=None) -> Response:
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "X-GitHub-Request-Id": store.next_request_id()}
    if extra_headers:
        headers.update(extra_headers)
    return Response(status, body, headers)


def error_response(store, err: ForgeError) -> Response:
    return json_response(store, err.status, {"message": err.message})


class Router:
    """Method + regex routes. Handler signature: fn(request, **groups) -> Response."""

    def __init__(self):
        self.routes = []  # (method, compiled_regex, handler)

    def add(self, method: str, pattern: str, handler):
        regex = re.compile("^" + pattern + "$")
        self.routes.append((method, regex, handler))

    def dispatch(self, request: Request) -> Response:
        for method, regex, handler in self.routes:
            if method != request.method:
                continue
            m = regex.match(request.path)
            if m:
                return handler(request, **m.groupdict())
        raise ForgeError(404, "Not found")


def make_handler(store, router, auth_token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "forge/1.0"

        def log_message(self, *args):
            pass

        # -- helpers ------------------------------------------------------
        def _read_body(self) -> bytes:
            length = self.headers.get("Content-Length")
            if length is not None:
                try:
                    n = int(length)
                except ValueError:
                    return b""
                if n <= 0:
                    return b""
                return self.rfile.read(n)
            if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
                chunks = []
                while True:
                    line = self.rfile.readline(65536).strip()
                    try:
                        size = int(line.split(b";")[0], 16)
                    except ValueError:
                        break
                    if size == 0:
                        self.rfile.readline(65536)
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline(65536)
                return b"".join(chunks)
            return b""

        def _handle(self, method: str):
            try:
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                query = parse_qs(parsed.query)
                headers = {k.lower(): v for k, v in self.headers.items()}
                body = self._read_body()
                request = Request(method, path, query, headers, body,
                                  self.headers.get("Host", ""), store)
                response = router.dispatch(request)
            except ForgeError as err:
                response = error_response(store, err)
            except BrokenPipeError:
                raise
            except Exception as err:  # never leak a stack trace or HTML
                response = error_response(
                    store, ForgeError(500, f"internal error: {err}"))
            try:
                self.send_response(response.status)
                self.send_header("Content-Length", str(len(response.body)))
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if response.body and method != "HEAD":
                    self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

        def do_HEAD(self):
            self._handle("HEAD")

    return Handler


def serve(store, router, host: str = "0.0.0.0", port: int = 0):
    handler = make_handler(store, router, None)
    return ForgeHTTPServer((host, port), handler)


def check_auth(request: Request, token: str | None) -> None:
    """Bearer auth for API surfaces. No token configured -> open (local dev).
    Missing or wrong token -> 401."""
    if token is None:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {token}":
        raise ForgeError(401, "Bad credentials")
