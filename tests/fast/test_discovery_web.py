from __future__ import annotations

import asyncio
import dataclasses
import re

import pytest

from personal_voice_msg.discovery.web import (
    DiscoveryBoundaryError,
    DiscoveryWebSession,
    SearchHit,
    SearchResult,
    canonical_public_url,
    is_public_address,
)

MAX_SEARCH_RESULTS = 20
MAX_TITLE_CHARACTERS = 200
MAX_SNIPPET_CHARACTERS = 500


def search_hit(
    number: int = 0,
    *,
    url: str | None = None,
    title: str | None = None,
    snippet: str | None = None,
) -> SearchHit:
    return SearchHit(
        url=url or f"https://source-{number}.example/article/{number}?private=1#quote",
        title=title or f"Result {number}",
        snippet=snippet or f"Snippet {number}",
    )


@pytest.mark.fast
@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("http://example.com", "http://example.com/"),
        ("http://EXAMPLE.com:80/path#section", "http://example.com/path"),
        (
            "https://EXAMPLE.com:443/a/b?q=private#fragment",
            "https://example.com/a/b?q=private",
        ),
        ("https://8.8.8.8/dns#fragment", "https://8.8.8.8/dns"),
        (
            "https://[2606:4700:4700::1111]/dns#fragment",
            "https://[2606:4700:4700::1111]/dns",
        ),
    ],
)
def test_canonical_public_url_accepts_only_unambiguous_public_http_urls(
    raw_url: str,
    expected: str,
) -> None:
    assert canonical_public_url(raw_url) == expected


@pytest.mark.fast
@pytest.mark.parametrize(
    "raw_url",
    [
        "",
        "example.com/no-scheme",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://example.com/1",
        "data:text/plain,hello",
        "http:///missing-host",
        "http://example.com:443/",
        "https://example.com:80/",
        "https://example.com:444/",
        "https://user@example.com/",
        "https://user:password@example.com/",
        "https://example.com/path\nhttps://169.254.169.254/",
        "https://exam\x00ple.com/",
        "https://exam\u2003ple.com/",
        "https://example.com/path\u00a0suffix",
        "https://example.com/\u3000",
        "https://localhost/",
        "https://example..com/",
        "https://-bad.example/",
        "https://bad-.example/",
        "https://[fe80::1%25eth0]/",
        "https://2130706433/",
        "https://0177.0.0.1/",
        "https://0x7f000001/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "https://[::1]/",
    ],
)
def test_canonical_public_url_rejects_unsafe_or_ambiguous_grammar(
    raw_url: str,
) -> None:
    with pytest.raises(DiscoveryBoundaryError):
        canonical_public_url(raw_url)


@pytest.mark.fast
@pytest.mark.parametrize(
    "address",
    [
        "1.1.1.1",
        "8.8.8.8",
        "93.184.216.34",
        "2606:4700:4700::1111",
        "2001:4860:4860::8888",
    ],
)
def test_is_public_address_accepts_native_globally_routable_addresses(
    address: str,
) -> None:
    assert is_public_address(address)


@pytest.mark.fast
@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "100.100.100.200",
        "127.0.0.1",
        "169.254.169.254",
        "172.16.0.1",
        "192.168.0.1",
        "192.0.2.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",
        "2002:0808:0808::1",
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
    ],
)
def test_is_public_address_rejects_non_global_mapped_and_tunnel_addresses(
    address: str,
) -> None:
    assert not is_public_address(address)


@pytest.mark.fast
@pytest.mark.parametrize("address", ["", "not-an-address", "8.8.8.8:53", "[::1]"])
def test_is_public_address_rejects_malformed_address_text(address: str) -> None:
    assert not is_public_address(address)


@pytest.mark.fast
def test_record_search_results_returns_only_sanitized_bounded_metadata() -> None:
    session = DiscoveryWebSession()
    raw_url = "https://Example.COM/private/path?q=secret#private-fragment"
    title = "  <b>Ｆｕｌｌ&nbsp;Ｔｉｔｌｅ</b>\x00\u202e  " + "T" * 300
    snippet = " <i>Warm&nbsp; words</i>\r\n\t " + "S" * 700

    results = session.record_search_results(
        (search_hit(url=raw_url, title=title, snippet=snippet),)
    )

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, SearchResult)
    assert result.title.startswith("Full Title ")
    assert "<b>" not in result.title
    assert "&nbsp;" not in result.title
    assert "\x00" not in result.title
    assert "\u202e" not in result.title
    assert "\n" not in result.snippet
    assert "\t" not in result.snippet
    assert "  " not in result.title
    assert "  " not in result.snippet
    assert len(result.title) == MAX_TITLE_CHARACTERS
    assert len(result.snippet) == MAX_SNIPPET_CHARACTERS
    assert result.display_hostname == "example.com"

    public_fields = {field.name for field in dataclasses.fields(result)}
    assert public_fields == {"result_id", "title", "snippet", "display_hostname"}
    rendered = " ".join(str(getattr(result, field)) for field in public_fields)
    assert raw_url not in rendered
    assert "https://" not in rendered
    assert "/private/path" not in rendered
    assert "q=secret" not in rendered
    assert "private-fragment" not in rendered


@pytest.mark.fast
def test_record_search_results_accepts_the_exact_result_limit() -> None:
    session = DiscoveryWebSession()
    hits = tuple(search_hit(number) for number in range(MAX_SEARCH_RESULTS))

    results = session.record_search_results(hits)

    assert len(results) == MAX_SEARCH_RESULTS


@pytest.mark.fast
def test_record_search_results_rejects_over_limit_atomically() -> None:
    session = DiscoveryWebSession()
    too_many = tuple(search_hit(number) for number in range(MAX_SEARCH_RESULTS + 1))

    with pytest.raises(DiscoveryBoundaryError):
        session.record_search_results(too_many)

    results = session.record_search_results((search_hit(100),))
    assert len(results) == 1
    assert results[0].title == "Result 100"


@pytest.mark.fast
@pytest.mark.parametrize(
    ("field", "raw_value", "expected_length"),
    [
        ("title", "<b>" + "T" * 1_994 + "</b>", 2_001),
        ("snippet", "<i>" + "S" * 4_994 + "</i>", 5_001),
    ],
)
def test_raw_metadata_input_limits_reject_generically_and_atomically(
    field: str,
    raw_value: str,
    expected_length: int,
) -> None:
    assert len(raw_value) == expected_length
    session = DiscoveryWebSession()
    invalid_hit = (
        search_hit(1, title=raw_value)
        if field == "title"
        else search_hit(2, snippet=raw_value)
    )

    with pytest.raises(DiscoveryBoundaryError) as raised:
        session.record_search_results((invalid_hit,))

    assert str(raised.value)
    assert field not in str(raised.value).lower()
    valid_results = session.record_search_results((search_hit(100),))
    assert len(valid_results) == 1


@pytest.mark.fast
def test_result_ids_are_unique_cryptographically_opaque_and_url_private() -> None:
    session = DiscoveryWebSession()
    hits = tuple(search_hit(number) for number in range(MAX_SEARCH_RESULTS))

    results = session.record_search_results(hits)
    result_ids = [result.result_id for result in results]

    assert len(set(result_ids)) == MAX_SEARCH_RESULTS
    assert all(
        re.fullmatch(r"[A-Za-z0-9_-]{22,}", result_id)
        for result_id in result_ids
    )
    for hit, result_id in zip(hits, result_ids, strict=True):
        assert result_id != hit.url
        assert "source-" not in result_id
        assert "example" not in result_id
        assert "/" not in result_id
        assert "?" not in result_id
        assert "#" not in result_id


def fetch_boundary_error(
    session: DiscoveryWebSession,
    result_id: str,
) -> DiscoveryBoundaryError:
    async def fetch() -> None:
        await session.fetch_public_page(result_id)

    with pytest.raises(DiscoveryBoundaryError) as raised:
        asyncio.run(fetch())
    return raised.value


@pytest.mark.fast
def test_unknown_cross_run_and_closed_ids_share_one_generic_failure() -> None:
    source_session = DiscoveryWebSession()
    other_session = DiscoveryWebSession()
    closed_session = DiscoveryWebSession()
    source_result = source_session.record_search_results((search_hit(1),))[0]
    closed_result = closed_session.record_search_results((search_hit(2),))[0]
    closed_session.close()

    unknown_error = fetch_boundary_error(source_session, "not-a-real-result-id")
    cross_run_error = fetch_boundary_error(other_session, source_result.result_id)
    closed_error = fetch_boundary_error(closed_session, closed_result.result_id)

    assert str(unknown_error)
    assert type(unknown_error) is type(cross_run_error) is type(closed_error)
    assert str(unknown_error) == str(cross_run_error) == str(closed_error)


@pytest.mark.fast
def test_altered_result_id_is_not_accepted() -> None:
    session = DiscoveryWebSession()
    result = session.record_search_results((search_hit(1),))[0]

    original_error = fetch_boundary_error(session, "not-a-real-result-id")
    altered_error = fetch_boundary_error(session, result.result_id + "A")

    assert str(altered_error) == str(original_error)
