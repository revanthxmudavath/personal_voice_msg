from __future__ import annotations

import asyncio
import os
import urllib.request

import pytest

from personal_voice_msg.discovery.web import (
    DiscoveryBoundaryError,
    DiscoveryWebSession,
    FetchedPage,
    FetchPolicy,
    SearchHit,
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


def fixture_read(url: str) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:
        return response.read()


def canary_count(origin: str) -> int:
    return int(fixture_read(f"{origin}/count").decode())


def test_fetches_public_fixture_without_ambient_proxy_or_netrc() -> None:
    page = fetch_path("/request-info")

    assert page.media_type == "text/plain"
    assert b"host=public.fixture.example" in page.body
    assert b"authorization=\n" in page.body
    assert b"cookie=\n" in page.body
    assert (
        b"user-agent=personal-voice-message-bot/0.1 "
        b"(https://github.com/revanthxmudavath)\n"
    ) in page.body


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
    before = canary_count("http://private.fixture.example")

    rejected_path("/redirect-private")

    assert canary_count("http://private.fixture.example") == before


def test_mixed_public_private_dns_answer_is_rejected_before_request() -> None:
    before = canary_count("http://private.fixture.example")
    session = DiscoveryWebSession()
    result = session.record_search_results(
        (SearchHit("http://mixed.fixture.example/ok", "mixed", "mixed"),)
    )[0]

    with pytest.raises(DiscoveryBoundaryError) as raised:
        asyncio.run(session.fetch_public_page(result.result_id))

    assert str(raised.value) == BOUNDARY_MESSAGE
    assert canary_count("http://private.fixture.example") == before


def test_azure_redirect_is_rejected_before_canary_request() -> None:
    before = canary_count("http://168.63.129.16")

    rejected_path("/redirect-azure")

    assert canary_count("http://168.63.129.16") == before


def test_fetch_rejects_dns_rebinding_before_rebound_request() -> None:
    async def exercise() -> None:
        rebound_origin = "http://untrusted.fixture.example"
        before = await asyncio.to_thread(canary_count, rebound_origin)
        session = DiscoveryWebSession()
        result = session.record_search_results(
            (
                SearchHit(
                    "http://rebind.fixture.example/redirect-rebind",
                    "rebind",
                    "rebind",
                ),
            )
        )[0]
        fetch_task = asyncio.create_task(
            session.fetch_public_page(result.result_id)
        )
        for _ in range(40):
            ready = await asyncio.to_thread(
                fixture_read,
                f"{PUBLIC_ORIGIN}/rebind-ready",
            )
            if ready == b"1":
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("permitted redirect fixture did not receive first request")

        with open("/etc/hosts", "a", encoding="ascii") as hosts_file:
            hosts_file.write("\n93.184.216.11 rebind.fixture.example.\n")
        await asyncio.to_thread(
            fixture_read,
            f"{PUBLIC_ORIGIN}/release-rebind",
        )

        with pytest.raises(DiscoveryBoundaryError) as raised:
            await fetch_task
        assert str(raised.value) == BOUNDARY_MESSAGE
        assert await asyncio.to_thread(canary_count, rebound_origin) == before

    asyncio.run(exercise())


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
