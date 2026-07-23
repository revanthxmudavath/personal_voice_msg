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

pytestmark = pytest.mark.security


def fetch(session: DiscoveryWebSession, result_id: str) -> FetchedPage:
    return asyncio.run(session.fetch_public_page(result_id))


def assert_boundary_error(error: pytest.ExceptionInfo[DiscoveryBoundaryError]) -> None:
    assert str(error.value) == BOUNDARY_MESSAGE


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://[::1]/",
        "http://[fe80::1]/",
    ],
    ids=(
        "localhost-name",
        "localhost-ipv4",
        "private-ipv4",
        "link-local-ipv4",
        "aws-metadata",
        "alibaba-metadata",
        "localhost-ipv6",
        "link-local-ipv6",
    ),
)
def test_literal_non_public_search_results_fail_closed(url: str) -> None:
    session = DiscoveryWebSession()

    with pytest.raises(DiscoveryBoundaryError) as error:
        session.record_search_results((SearchHit(url, "title", "snippet"),))

    assert_boundary_error(error)


@pytest.mark.parametrize(
    "forged_id",
    [
        "https://example.com/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "not-an-issued-result-id",
    ],
    ids=("public-url", "localhost", "private", "metadata", "opaque-looking"),
)
def test_forged_or_url_shaped_result_ids_fail_closed(forged_id: str) -> None:
    session = DiscoveryWebSession()

    with pytest.raises(DiscoveryBoundaryError) as error:
        fetch(session, forged_id)

    assert_boundary_error(error)


def test_result_id_from_another_discovery_run_fails_closed() -> None:
    first_run = DiscoveryWebSession()
    second_run = DiscoveryWebSession()
    result = first_run.record_search_results(
        (SearchHit("https://example.com/", "title", "snippet"),)
    )[0]

    with pytest.raises(DiscoveryBoundaryError) as error:
        fetch(second_run, result.result_id)

    assert_boundary_error(error)
