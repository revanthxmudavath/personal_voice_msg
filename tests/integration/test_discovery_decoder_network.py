from __future__ import annotations

import asyncio
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from personal_voice_msg.discovery.baseline import (
    DISCOVERY_QUERIES,
    MAX_SEARCH_JSON_DEPTH,
    DeterministicDiscovery,
    DiscoverySearchError,
)
from personal_voice_msg.discovery.web import (
    MAX_SEARCH_RESULTS,
    DiscoveryWebSession,
    SearchHit,
    SearchResult,
)

pytestmark = pytest.mark.integration

DEEP_JSON = (
    b'{"results":'
    + b"[" * MAX_SEARCH_JSON_DEPTH
    + b"0"
    + b"]" * MAX_SEARCH_JSON_DEPTH
    + b"}"
)
AT_LIMIT_JSON = (
    b'{"results":'
    + b"[" * (MAX_SEARCH_JSON_DEPTH - 1)
    + b"0"
    + b"]" * (MAX_SEARCH_JSON_DEPTH - 1)
    + b"}"
)
STRING_DELIMITERS_JSON = json.dumps(
    {
        "results": [
            {
                "url": "https://standardebooks.org/ebooks/example",
                "title": (
                    "[{" * (MAX_SEARCH_JSON_DEPTH + 1)
                    + ' escaped quote: " and consecutive slashes: \\\\'
                    + "}]" * (MAX_SEARCH_JSON_DEPTH + 1)
                ),
                "content": "Valid delimiters inside a JSON string.",
            }
        ]
    }
).encode()
OVERSIZED_INTEGER_JSON = (
    b'{"results":[],"pathological_number":' + b"9" * 4_301 + b"}"
)
UTF16_DEEP_JSON = (
    '{"prefix":"\\\\\\"","results":'
    + "[" * (MAX_SEARCH_JSON_DEPTH + 1)
    + "0"
    + "]" * (MAX_SEARCH_JSON_DEPTH + 1)
    + ',"suffix":"\\\\\\""}'
).encode("utf-16")
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


class _AtLimitJsonHandler(_DeepJsonHandler):
    body = AT_LIMIT_JSON


class _StringDelimitersJsonHandler(_DeepJsonHandler):
    body = STRING_DELIMITERS_JSON


class _OversizedIntegerJsonHandler(_DeepJsonHandler):
    body = OVERSIZED_INTEGER_JSON


class _Utf16DeepJsonHandler(_DeepJsonHandler):
    body = UTF16_DEEP_JSON


def _search_response(
    handler: type[BaseHTTPRequestHandler],
) -> tuple[SearchResult, ...]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address
    discovery = DeterministicDiscovery(
        f"http://{host}:{port}",
        DiscoveryWebSession(),
    )
    try:
        return asyncio.run(discovery.search_web(DISCOVERY_QUERIES[0]))
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


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


def test_json_at_the_depth_limit_remains_decodable() -> None:
    assert _search_response(_AtLimitJsonHandler) == ()


def test_structural_characters_inside_json_strings_do_not_count_as_depth() -> None:
    results = _search_response(_StringDelimitersJsonHandler)

    assert len(results) == 1
    assert results[0].display_hostname == "standardebooks.org"


def test_oversized_json_integer_is_rejected_generically() -> None:
    with pytest.raises(DiscoverySearchError, match="search response rejected"):
        _search_response(_OversizedIntegerJsonHandler)


def test_utf16_cannot_bypass_the_json_depth_limit() -> None:
    with pytest.raises(DiscoverySearchError, match="search response rejected"):
        _search_response(_Utf16DeepJsonHandler)
