from __future__ import annotations

import asyncio

import pytest

from personal_voice_msg.discovery.web import (
    DiscoveryBoundaryError,
    DiscoveryWebSession,
    FetchedPage,
    FetchPolicy,
    SearchHit,
    canonical_public_url,
    is_public_address,
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
        "http://168.63.129.16/",
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
        "azure-wireserver",
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


def test_azure_wireserver_address_is_not_public() -> None:
    assert not is_public_address("168.63.129.16")
    with pytest.raises(DiscoveryBoundaryError) as error:
        canonical_public_url("http://168.63.129.16/")
    assert_boundary_error(error)


COUNT_FIELDS = (
    "max_body_bytes",
    "max_redirects",
    "max_headers",
    "max_line_size",
    "max_field_size",
)
DURATION_FIELDS = (
    "total_timeout_seconds",
    "dns_timeout_seconds",
    "connect_timeout_seconds",
    "read_timeout_seconds",
)


@pytest.mark.parametrize("field_name", COUNT_FIELDS)
@pytest.mark.parametrize(
    "value",
    [1.0, True, float("nan"), float("inf"), float("-inf")],
    ids=("float", "bool", "nan", "positive-infinity", "negative-infinity"),
)
def test_fetch_policy_count_fields_require_actual_positive_integers(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        FetchPolicy(**{field_name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", DURATION_FIELDS)
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_fetch_policy_duration_fields_reject_non_finite_values(
    field_name: str,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="finite durations"):
        FetchPolicy(**{field_name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", DURATION_FIELDS)
@pytest.mark.parametrize("value", [1, 0.25], ids=("integer", "float"))
def test_fetch_policy_duration_fields_accept_positive_finite_numbers(
    field_name: str,
    value: int | float,
) -> None:
    FetchPolicy(**{field_name: value})  # type: ignore[arg-type]
