from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", maxsplit=1)[0]
        if path == "/health":
            self._send(200, b"ok", "text/plain")
            return
        if path == "/engine":
            body = json.dumps(
                {
                    "results": [
                        {
                            "url": "http://public.fixture.example/article",
                            "title": "Quiet affection",
                            "content": "A deterministic, public fixture result.",
                        },
                        {
                            "url": "http://unapproved.fixture.example/ignored",
                            "title": "Outside the curated source list",
                            "content": (
                                "This result must not become a fetch capability."
                            ),
                        },
                    ]
                }
            ).encode()
            self._send(200, body, "application/json")
            return
        if path == "/article":
            body = b"""
            <html><body><main>
            <h1>Quiet affection</h1>
            <p>Kindness can make an ordinary morning feel deeply cared for.</p>
            <p>Gentle attention turns small moments into lasting warmth.</p>
            </main></body></html>
            """
            self._send(200, body, "text/html")
            return
        if path == "/empty":
            self._send(
                200,
                b"<html><body><script>ignored()</script></body></html>",
                "text/html",
            )
            return
        self._send(404, b"not found", "text/plain")


ThreadingHTTPServer(("0.0.0.0", 80), FixtureHandler).serve_forever()
