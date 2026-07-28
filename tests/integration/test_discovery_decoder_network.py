from __future__ import annotations

import asyncio
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from personal_voice_msg.discovery.baseline import (
    DISCOVERY_QUERIES,
    DeterministicDiscovery,
    DiscoverySearchError,
)
from personal_voice_msg.discovery.web import (
    MAX_SEARCH_RESULTS,
    DiscoveryWebSession,
    SearchHit,
)

pytestmark = pytest.mark.integration

DEEP_JSON = b'{"results":' + b"[" * 5_000 + b"0" + b"]" * 5_000 + b"}"
DISTINCTIVE_SOURCE_PHRASE = "violet lanterns guarded the malformed harbor"
MALFORMED_JSON = (
    b'{"results":[{"content":"'
    + DISTINCTIVE_SOURCE_PHRASE.encode()
    + b'"}'
)


class _DeepJsonHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    body = DEEP_JSON

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


class _MalformedJsonHandler(_DeepJsonHandler):
    body = MALFORMED_JSON


def test_malformed_json_discards_the_raw_exception_chain() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MalformedJsonHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    web_session = DiscoveryWebSession()
    discovery = DeterministicDiscovery(
        f"http://{host}:{port}",
        web_session,
    )

    try:
        with pytest.raises(DiscoverySearchError) as raised:
            asyncio.run(discovery.search_web(DISCOVERY_QUERIES[0]))
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    exception = raised.value
    rendered_traceback = "".join(
        traceback.format_exception(
            type(exception),
            exception,
            exception.__traceback__,
        )
    )
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert str(exception) == "search response rejected"
    assert repr(exception) == "DiscoverySearchError('search response rejected')"
    assert DISTINCTIVE_SOURCE_PHRASE not in rendered_traceback

    capacity_probe = tuple(
        SearchHit(
            url=f"https://example.com/{index}",
            title=f"probe {index}",
            snippet="decoder failure must register no results",
        )
        for index in range(MAX_SEARCH_RESULTS)
    )
    assert len(web_session.record_search_results(capacity_probe)) == MAX_SEARCH_RESULTS


def test_pathological_json_fails_before_any_result_registration() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeepJsonHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    web_session = DiscoveryWebSession()
    discovery = DeterministicDiscovery(
        f"http://{host}:{port}",
        web_session,
    )

    try:
        with pytest.raises(DiscoverySearchError, match="search response rejected"):
            asyncio.run(discovery.search_web(DISCOVERY_QUERIES[0]))
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    capacity_probe = tuple(
        SearchHit(
            url=f"https://example.com/{index}",
            title=f"probe {index}",
            snippet="decoder failure must register no results",
        )
        for index in range(MAX_SEARCH_RESULTS)
    )
    assert len(web_session.record_search_results(capacity_probe)) == MAX_SEARCH_RESULTS
