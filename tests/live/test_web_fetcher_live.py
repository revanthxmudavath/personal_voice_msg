from __future__ import annotations

import asyncio

import pytest

from personal_voice_msg.discovery.web import (
    DiscoveryBoundaryError,
    DiscoveryWebSession,
    FetchedPage,
    SearchHit,
)

BOUNDARY_MESSAGE = "discovery web boundary rejected the request"

pytestmark = pytest.mark.live


def fetch(session: DiscoveryWebSession, result_id: str) -> FetchedPage:
    return asyncio.run(session.fetch_public_page(result_id))


def assert_boundary_error(error: pytest.ExceptionInfo[DiscoveryBoundaryError]) -> None:
    assert str(error.value) == BOUNDARY_MESSAGE


def test_fetches_bounded_example_page_over_real_https() -> None:
    session = DiscoveryWebSession()
    result = session.record_search_results(
        (
            SearchHit(
                url="https://example.com/",
                title="Example Domain",
                snippet="A stable public HTTPS fixture.",
            ),
        )
    )[0]

    page = fetch(session, result.result_id)

    assert page.result_id == result.result_id
    assert page.final_url == "https://example.com/"
    assert page.media_type == "text/html"
    assert b"Example Domain" in page.body
    assert 0 < len(page.body) <= 1_048_576


def test_real_public_unsupported_content_type_fails_closed() -> None:
    session = DiscoveryWebSession()
    result = session.record_search_results(
        (SearchHit("https://httpbin.org/image/png", "image", "binary fixture"),)
    )[0]

    with pytest.raises(DiscoveryBoundaryError) as error:
        fetch(session, result.result_id)

    assert_boundary_error(error)


def test_real_public_redirect_to_private_address_fails_closed() -> None:
    session = DiscoveryWebSession()
    result = session.record_search_results(
        (
            SearchHit(
                "https://httpbin.org/redirect-to?url=http%3A%2F%2F127.0.0.1%2F",
                "redirect",
                "private redirect fixture",
            ),
        )
    )[0]

    with pytest.raises(DiscoveryBoundaryError) as error:
        fetch(session, result.result_id)

    assert_boundary_error(error)


@pytest.mark.parametrize(
    "url",
    [
        "https://self-signed.badssl.com/",
        "https://wrong.host.badssl.com/",
    ],
    ids=("self-signed-certificate", "wrong-host-certificate"),
)
def test_real_invalid_tls_certificate_fails_closed(url: str) -> None:
    session = DiscoveryWebSession()
    result = session.record_search_results(
        (SearchHit(url, "invalid TLS", "certificate fixture"),)
    )[0]

    with pytest.raises(DiscoveryBoundaryError) as error:
        fetch(session, result.result_id)

    assert_boundary_error(error)
