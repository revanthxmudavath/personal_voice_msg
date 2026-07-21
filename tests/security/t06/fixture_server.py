from __future__ import annotations

import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 1_048_576
CANARY_LOCK = threading.Lock()
CANARY_HITS = 0


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes = b"",
        *,
        content_type: str | None = "text/html",
        headers: tuple[tuple[str, str], ...] = (),
        content_length: int | None = None,
    ) -> None:
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        for name, value in headers:
            self.send_header(name, value)
        length = len(body) if content_length is None else content_length
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _public_get(self) -> None:
        path = self.path.split("?", maxsplit=1)[0]
        if path in {"/", "/health", "/ok"}:
            self._send(200, b"<html><body>public fixture</body></html>")
            return
        if path == "/request-info":
            body = (
                f"host={self.headers.get('Host', '')}\n"
                f"authorization={self.headers.get('Authorization', '')}\n"
                f"cookie={self.headers.get('Cookie', '')}\n"
            ).encode()
            self._send(200, body, content_type="text/plain")
            return
        if path == "/redirect-relative":
            self._send(302, headers=(("Location", "/ok"),))
            return
        if path == "/redirect-private":
            self._send(
                302,
                headers=(("Location", "http://private.fixture.example/canary"),),
            )
            return
        if path == "/https-downgrade":
            self._send(
                302,
                headers=(("Location", "http://public.fixture.example/ok"),),
            )
            return
        if path == "/malformed-location":
            self._send(302, headers=(("Location", "http://[::1"),))
            return
        if path == "/oversized-location":
            location = f"http://{'a' * 2_100}.example/"
            self._send(302, headers=(("Location", location),))
            return
        if path == "/redirect-loop-a":
            self._send(302, headers=(("Location", "/redirect-loop-b"),))
            return
        if path == "/redirect-loop-b":
            self._send(302, headers=(("Location", "/redirect-loop-a"),))
            return
        if path == "/duplicate-location":
            self._send(
                302,
                headers=(("Location", "/ok"), ("Location", "/health")),
            )
            return
        if path.startswith("/redirect/"):
            remaining = int(path.rsplit("/", maxsplit=1)[1])
            if remaining == 0:
                self._send(200, b"redirect complete", content_type="text/plain")
                return
            self._send(
                302,
                headers=(("Location", f"/redirect/{remaining - 1}"),),
            )
            return
        if path.startswith("/body/"):
            size = int(path.rsplit("/", maxsplit=1)[1])
            self._send(200, b"x" * size, content_type="text/plain")
            return
        if path == "/oversized-length":
            self._send(
                200,
                content_type="text/plain",
                content_length=MAX_BODY_BYTES + 1,
            )
            return
        if path == "/chunked-overflow":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(17):
                    chunk = b"x" * 65_536
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path == "/slow":
            time.sleep(1.0)
            self._send(200, b"slow", content_type="text/plain")
            return
        if path == "/slow-stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            try:
                self.wfile.write(b"a")
                self.wfile.flush()
                time.sleep(1.0)
                self.wfile.write(b"b")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path == "/premature-eof":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"short")
            self.wfile.flush()
            self.close_connection = True
            return
        if path == "/unsupported":
            self._send(200, b"{}", content_type="application/json")
            return
        if path == "/missing-content-type":
            self._send(200, b"missing", content_type=None)
            return
        if path == "/duplicate-content-type":
            self._send(
                200,
                b"duplicate",
                content_type="text/plain",
                headers=(("Content-Type", "text/html"),),
            )
            return
        if path == "/malformed-content-type":
            self._send(200, b"malformed", content_type="text/html; invalid")
            return
        if path == "/compressed":
            self._send(
                200,
                b"not actually compressed",
                content_type="text/plain",
                headers=(("Content-Encoding", "gzip"),),
            )
            return
        if path == "/many-headers":
            headers = tuple((f"X-Fixture-{index}", "value") for index in range(70))
            self._send(200, b"headers", content_type="text/plain", headers=headers)
            return
        if path == "/oversized-header-field":
            self._send(
                200,
                b"header",
                content_type="text/plain",
                headers=(("X-Oversized", "x" * 9_000),),
            )
            return
        if path == "/te-and-cl":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "1")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                self.wfile.write(b"1\r\nx\r\n0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._send(404, b"not found", content_type="text/plain")

    def _canary_get(self) -> None:
        global CANARY_HITS
        path = self.path.split("?", maxsplit=1)[0]
        if path == "/count":
            with CANARY_LOCK:
                count = CANARY_HITS
            self._send(200, str(count).encode(), content_type="text/plain")
            return
        with CANARY_LOCK:
            CANARY_HITS += 1
        self._send(200, b"private canary", content_type="text/plain")

    def do_GET(self) -> None:
        if os.environ.get("FIXTURE_MODE") == "canary":
            self._canary_get()
        else:
            self._public_get()


http_server = ThreadingHTTPServer(("0.0.0.0", 80), FixtureHandler)
if os.environ.get("FIXTURE_TLS"):
    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    https_server = ThreadingHTTPServer(("0.0.0.0", 443), FixtureHandler)
    tls_mode = os.environ["FIXTURE_TLS"]
    certificate = (
        "/workspace/tls/server.crt"
        if tls_mode == "trusted"
        else "/workspace/tls/untrusted.crt"
    )
    private_key = (
        "/workspace/tls/server.key"
        if tls_mode == "trusted"
        else "/workspace/tls/untrusted.key"
    )
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(certificate, private_key)
    https_server.socket = tls_context.wrap_socket(
        https_server.socket,
        server_side=True,
    )
    https_server.serve_forever()
else:
    http_server.serve_forever()
