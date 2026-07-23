from __future__ import annotations

import asyncio
import os
import socket
import urllib.request

import pytest

from personal_voice_msg.discovery.web import (
    DiscoveryBoundaryError,
    DiscoveryWebSession,
    FetchedPage,
    FetchPolicy,
    SearchHit,
    _PublicResolver,
)

PUBLIC_ORIGIN = "http://public.fixture.example"
BOUNDARY_MESSAGE = "discovery web boundary rejected the request"

pytestmark = pytest.mark.security

if os.environ.get("T06_NETWORK_HARNESS") != "1":
    pytestmark = [
        pytest.mark.security,
        pytest.mark.integration,
        pytest.mark.skip(reason="requires the isolated T06 Docker network"),
    ]


def fetch_path(path: str, policy: FetchPolicy | None = None) -> FetchedPage:
    return fetch_url(f"{PUBLIC_ORIGIN}{path}", policy)


def fetch_url(url: str, policy: FetchPolicy | None = None) -> FetchedPage:
    session = DiscoveryWebSession(policy)
    result = session.record_search_results(
        (SearchHit(url, "fixture", "fixture"),)
    )[0]
    return asyncio.run(session.fetch_public_page(result.result_id))


def rejected_path(path: str, policy: FetchPolicy | None = None) -> None:
    with pytest.raises(DiscoveryBoundaryError) as raised:
        fetch_path(path, policy)
    assert str(raised.value) == BOUNDARY_MESSAGE


def private_canary_count() -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://private.fixture.example/count", timeout=2) as response:
        return int(response.read().decode())


def test_fetches_public_fixture_without_ambient_proxy_or_netrc() -> None:
    page = fetch_path("/request-info")

    assert page.media_type == "text/plain"
    assert b"host=public.fixture.example" in page.body
    assert b"authorization=\n" in page.body
    assert b"cookie=\n" in page.body


def test_controlled_tls_preserves_original_host_and_sni() -> None:
    page = fetch_url("https://public.fixture.example/request-info")

    assert page.media_type == "text/plain"
    assert b"host=public.fixture.example" in page.body


@pytest.mark.parametrize(
    "url",
    [
        "https://wrong.fixture.example/request-info",
        "https://untrusted.fixture.example/request-info",
    ],
    ids=("wrong-host", "untrusted-ca"),
)
def test_controlled_tls_identity_failures_are_rejected(url: str) -> None:
    with pytest.raises(DiscoveryBoundaryError) as raised:
        fetch_url(url)
    assert str(raised.value) == BOUNDARY_MESSAGE


def test_https_to_http_downgrade_is_rejected() -> None:
    with pytest.raises(DiscoveryBoundaryError) as raised:
        fetch_url("https://public.fixture.example/https-downgrade")
    assert str(raised.value) == BOUNDARY_MESSAGE


def test_relative_redirect_succeeds() -> None:
    page = fetch_path("/redirect-relative")

    assert page.final_url == f"{PUBLIC_ORIGIN}/ok"
    assert b"public fixture" in page.body


def test_private_redirect_is_rejected_before_canary_request() -> None:
    before = private_canary_count()

    rejected_path("/redirect-private")

    assert private_canary_count() == before


def test_mixed_public_private_dns_answer_is_rejected_before_request() -> None:
    before = private_canary_count()
    session = DiscoveryWebSession()
    result = session.record_search_results(
        (SearchHit("http://mixed.fixture.example/ok", "mixed", "mixed"),)
    )[0]

    with pytest.raises(DiscoveryBoundaryError) as raised:
        asyncio.run(session.fetch_public_page(result.result_id))

    assert str(raised.value) == BOUNDARY_MESSAGE
    assert private_canary_count() == before


def test_changed_public_name_service_answer_set_fails_closed() -> None:
    resolver = _PublicResolver(timeout_seconds=1.0)

    first = asyncio.run(
        resolver.resolve("rebind.fixture.example", 80, socket.AF_UNSPEC)
    )
    assert {answer["host"] for answer in first} == {"93.184.216.10"}

    with open("/etc/hosts", "a", encoding="ascii") as hosts_file:
        hosts_file.write("\n93.184.216.11 rebind.fixture.example.\n")

    with pytest.raises(OSError, match="destination resolution changed"):
        asyncio.run(
            resolver.resolve("rebind.fixture.example", 80, socket.AF_UNSPEC)
        )


@pytest.mark.parametrize(
    "path",
    [
        "/redirect-loop-a",
        "/duplicate-location",
        "/malformed-location",
        "/oversized-location",
        "/redirect/6",
    ],
    ids=(
        "loop",
        "duplicate-location",
        "malformed-location",
        "oversized-location",
        "over-limit",
    ),
)
def test_unsafe_redirect_chains_fail_closed(path: str) -> None:
    rejected_path(path)


def test_exact_redirect_limit_succeeds() -> None:
    page = fetch_path("/redirect/5")

    assert page.final_url == f"{PUBLIC_ORIGIN}/redirect/0"


def test_exact_body_limit_succeeds() -> None:
    page = fetch_path("/body/1048576")

    assert len(page.body) == 1_048_576


@pytest.mark.parametrize(
    "path",
    [
        "/oversized-length",
        "/chunked-overflow",
        "/unsupported",
        "/missing-content-type",
        "/duplicate-content-type",
        "/malformed-content-type",
        "/compressed",
        "/many-headers",
        "/oversized-header-field",
        "/premature-eof",
        "/te-and-cl",
    ],
    ids=(
        "content-length",
        "chunked-overflow",
        "media-type",
        "missing-media-type",
        "duplicate-media-type",
        "malformed-media-type",
        "compression",
        "header-count",
        "header-field-size",
        "premature-eof",
        "ambiguous-framing",
    ),
)
def test_invalid_or_oversized_responses_fail_closed(path: str) -> None:
    rejected_path(path)


def test_slow_response_exceeds_the_complete_deadline() -> None:
    policy = FetchPolicy(
        total_timeout_seconds=0.2,
        dns_timeout_seconds=0.2,
        connect_timeout_seconds=0.2,
        read_timeout_seconds=0.2,
    )

    rejected_path("/slow", policy)


def test_slow_stream_exceeds_the_idle_read_deadline() -> None:
    policy = FetchPolicy(
        total_timeout_seconds=2.0,
        dns_timeout_seconds=0.5,
        connect_timeout_seconds=0.5,
        read_timeout_seconds=0.2,
    )

    rejected_path("/slow-stream", policy)


def test_one_fetch_at_a_time_and_close_waits_for_completion() -> None:
    async def exercise() -> None:
        policy = FetchPolicy(
            total_timeout_seconds=2.0,
            dns_timeout_seconds=0.5,
            connect_timeout_seconds=0.5,
            read_timeout_seconds=1.5,
        )
        session = DiscoveryWebSession(policy)
        slow, second = session.record_search_results(
            (
                SearchHit(f"{PUBLIC_ORIGIN}/slow", "slow", "slow"),
                SearchHit(f"{PUBLIC_ORIGIN}/ok", "second", "second"),
            )
        )
        first_fetch = asyncio.create_task(
            session.fetch_public_page(slow.result_id)
        )
        await asyncio.sleep(0.1)

        with pytest.raises(DiscoveryBoundaryError):
            await session.fetch_public_page(second.result_id)
        with pytest.raises(DiscoveryBoundaryError):
            session.close()

        page = await first_fetch
        assert page.body == b"slow"
        session.close()

    asyncio.run(exercise())
