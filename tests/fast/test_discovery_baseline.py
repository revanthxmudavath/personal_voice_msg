from __future__ import annotations

import asyncio
import inspect
import traceback
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime, timedelta, timezone

import pytest

from personal_voice_msg.discovery import baseline
from personal_voice_msg.discovery.baseline import (
    CURATED_SOURCES,
    DISCOVERY_QUERIES,
    DeterministicDiscovery,
    DiscoveryExtractionError,
    DiscoveryRecord,
    DiscoverySearchError,
    SourceRule,
    analyze_fetched_page,
    parse_searxng_response,
)
from personal_voice_msg.discovery.web import (
    DiscoveryBoundaryError,
    DiscoveryWebSession,
    FetchedPage,
)

RULES = (
    SourceRule(
        hostname="public.fixture.example",
        rights_evidence="Public fixture content created for integration testing.",
    ),
)
RETRIEVED_AT = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
DISTINCTIVE_PHRASE = "violet lanterns guarded the quiet harbor"
EXPECTED_DISCOVERY_QUERIES = (
    "!sp standardebooks.org ebooks love romance",
    "!ws historic love poem song lyrics tenderness",
    "!wq love affection kindness tenderness quotations",
)


class _PassageRepr:
    def __init__(self, passage: str) -> None:
        self._passage = passage

    def __repr__(self) -> str:
        return self._passage


@pytest.mark.fast
def test_discovery_queries_are_fixed_bounded_and_target_curated_hosts() -> None:
    assert DISCOVERY_QUERIES == EXPECTED_DISCOVERY_QUERIES
    assert len(set(DISCOVERY_QUERIES)) == len(DISCOVERY_QUERIES)
    assert all(
        query.isascii() and 1 <= len(query) <= 200 for query in DISCOVERY_QUERIES
    )
    assert all("site:" not in query.casefold() for query in DISCOVERY_QUERIES)
    assert DISCOVERY_QUERIES[0].startswith("!sp ")
    assert DISCOVERY_QUERIES[1].startswith("!ws ")
    assert DISCOVERY_QUERIES[2].startswith("!wq ")


@pytest.mark.fast
def test_curated_source_evidence_never_certifies_item_rights() -> None:
    assert tuple(rule.hostname for rule in CURATED_SOURCES) == (
        "standardebooks.org",
        "en.wikisource.org",
        "en.wikiquote.org",
    )

    for rule in CURATED_SOURCES:
        evidence = rule.rights_evidence.casefold()
        assert "item-specific" in evidence
        assert "unknown" in evidence
        assert "certif" not in evidence


@pytest.mark.fast
def test_searxng_results_are_bounded_deduplicated_and_curated() -> None:
    payload = {
        "results": [
            {
                "url": "https://public.fixture.example/article",
                "title": "<b>Gentle</b> affection",
                "content": "A warm &amp; thoughtful result.",
            },
            {
                "url": "https://public.fixture.example/article",
                "title": "duplicate",
                "content": "duplicate",
            },
            {
                "url": "https://unapproved.example/article",
                "title": "not curated",
                "content": "must not become a capability",
            },
        ]
    }

    hits = parse_searxng_response(payload, RULES)

    assert len(hits) == 1
    assert hits[0].url == "https://public.fixture.example/article"
    assert hits[0].title == "<b>Gentle</b> affection"
    assert hits[0].snippet == "A warm &amp; thoughtful result."


@pytest.mark.fast
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"results": "not-a-list"},
    ],
)
def test_malformed_searxng_response_fails_closed(payload: object) -> None:
    with pytest.raises(DiscoverySearchError, match="search response"):
        parse_searxng_response(payload, RULES)


@pytest.mark.fast
def test_malformed_individual_results_are_ignored_without_losing_valid_hits() -> None:
    payload = {
        "results": [
            None,
            {"url": 7, "title": "title", "content": "content"},
            {"url": "https://public.fixture.example/missing-title"},
            {
                "url": "https://public.fixture.example/article",
                "title": "Valid result",
                "content": None,
            },
        ]
    }

    hits = parse_searxng_response(payload, RULES)

    assert len(hits) == 1
    assert hits[0].snippet == ""


@pytest.mark.fast
def test_search_rejects_queries_outside_the_fixed_baseline() -> None:
    discovery = DeterministicDiscovery(
        "http://trusted-searxng:8080",
        DiscoveryWebSession(),
        RULES,
    )

    with pytest.raises(DiscoverySearchError, match="query rejected"):
        asyncio.run(discovery.search_web("fetch an arbitrary topic"))


@pytest.mark.fast
def test_analyzer_output_cannot_escape_the_discovery_boundary() -> None:
    page = FetchedPage(
        result_id="opaque-result-id",
        final_url="https://public.fixture.example/article",
        media_type="text/html",
        body=(
            b"<html><body><main><p>"
            + DISTINCTIVE_PHRASE.encode()
            + b" while the morning stayed gentle and warm."
            b"</p></main></body></html>"
        ),
    )
    analyzer_calls: list[DiscoveryRecord] = []

    def return_text(text: str, record: DiscoveryRecord) -> object:
        analyzer_calls.append(record)
        return text

    def return_mapping(text: str, record: DiscoveryRecord) -> object:
        analyzer_calls.append(record)
        return {"passage": text}

    def return_object(text: str, record: DiscoveryRecord) -> object:
        analyzer_calls.append(record)
        return _PassageRepr(text)

    def return_nested(text: str, record: DiscoveryRecord) -> object:
        analyzer_calls.append(record)
        return {"nested": [{"passage": text}] * 10_000}

    adversarial_analyzers = (
        return_text,
        return_mapping,
        return_object,
        return_nested,
    )

    for analyzer in adversarial_analyzers:
        call_count = len(analyzer_calls)
        with pytest.raises(DiscoveryExtractionError) as raised:
            analyze_fetched_page(page, RETRIEVED_AT, RULES, analyzer)
        assert len(analyzer_calls) == call_count + 1
        assert str(raised.value) == "page extraction failed"
        assert DISTINCTIVE_PHRASE not in str(raised.value)
        assert DISTINCTIVE_PHRASE not in repr(raised.value)

    exception_calls: list[DiscoveryRecord] = []

    def leaking_exception(text: str, record: DiscoveryRecord) -> None:
        exception_calls.append(record)
        raise ValueError(
            f"{text} {record.source_url} {record.rights_evidence}"
        )

    with pytest.raises(DiscoveryExtractionError) as raised:
        analyze_fetched_page(page, RETRIEVED_AT, RULES, leaking_exception)
    assert len(exception_calls) == 1
    assert str(raised.value) == "page extraction failed"
    assert DISTINCTIVE_PHRASE not in repr(raised.value)
    assert exception_calls[0].source_url not in repr(raised.value)
    assert exception_calls[0].rights_evidence not in repr(raised.value)

    record = analyze_fetched_page(
        page,
        RETRIEVED_AT,
        RULES,
        lambda text, provenance: None,
    )
    assert not hasattr(record, "analysis")
    assert DISTINCTIVE_PHRASE not in repr(record)
    assert DISTINCTIVE_PHRASE not in repr(asdict(record))


@pytest.mark.fast
def test_analyzer_exception_discards_the_raw_exception_chain() -> None:
    page = FetchedPage(
        result_id="opaque-result-id",
        final_url="https://public.fixture.example/article",
        media_type="text/html",
        body=(
            b"<html><body><main><p>"
            + DISTINCTIVE_PHRASE.encode()
            + b" while the morning stayed gentle and warm."
            b"</p></main></body></html>"
        ),
    )

    analyzer_calls: list[DiscoveryRecord] = []

    def leaking_exception(text: str, record: DiscoveryRecord) -> None:
        analyzer_calls.append(record)
        raise ValueError(
            f"{text} {record.source_url} {record.rights_evidence}"
        )

    with pytest.raises(DiscoveryExtractionError) as raised:
        analyze_fetched_page(page, RETRIEVED_AT, RULES, leaking_exception)

    assert len(analyzer_calls) == 1
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
    assert DISTINCTIVE_PHRASE not in rendered_traceback
    assert analyzer_calls[0].source_url not in rendered_traceback
    assert analyzer_calls[0].rights_evidence not in rendered_traceback
    assert str(exception) == "page extraction failed"
    assert repr(exception) == "DiscoveryExtractionError('page extraction failed')"


@pytest.mark.fast
def test_plain_text_decode_exception_discards_the_raw_exception_chain() -> None:
    raw_marker = b"raw-body-violet-lanterns-guarded-the-harbor"
    body = raw_marker + b"\xff"
    page = FetchedPage(
        result_id="plain-result-id",
        final_url="https://public.fixture.example/invalid.txt",
        media_type="text/plain",
        body=body,
    )

    with pytest.raises(DiscoveryExtractionError) as raised:
        analyze_fetched_page(
            page,
            RETRIEVED_AT,
            RULES,
            lambda text, record: None,
        )

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
    assert raw_marker.decode() not in rendered_traceback
    assert repr(body) not in rendered_traceback
    assert str(exception) == "page extraction failed"
    assert repr(exception) == "DiscoveryExtractionError('page extraction failed')"


@pytest.mark.fast
def test_retrieval_time_is_constructor_owned_and_normalized_to_utc() -> None:
    constructor_parameters = inspect.signature(
        DeterministicDiscovery.__init__
    ).parameters
    analyze_parameters = inspect.signature(
        DeterministicDiscovery.analyze_result
    ).parameters

    assert "clock" in constructor_parameters
    assert "retrieved_at" not in analyze_parameters

    capture_retrieval_time = getattr(baseline, "_capture_retrieval_time")
    pacific_time = datetime(
        2026,
        7,
        27,
        5,
        0,
        tzinfo=timezone(-timedelta(hours=7)),
    )
    assert capture_retrieval_time(lambda: pacific_time) == datetime(
        2026,
        7,
        27,
        12,
        0,
        tzinfo=UTC,
    )

    for invalid_time in (object(), datetime(2026, 7, 27, 12, 0)):
        with pytest.raises(DiscoveryExtractionError, match="extraction failed"):
            capture_retrieval_time(lambda value=invalid_time: value)

    clock_calls: list[None] = []

    def counting_clock() -> datetime:
        clock_calls.append(None)
        return pacific_time

    discovery = DeterministicDiscovery(
        "http://trusted-searxng:8080",
        DiscoveryWebSession(),
        RULES,
        clock=counting_clock,
    )
    with pytest.raises(DiscoveryBoundaryError):
        asyncio.run(
            discovery.analyze_result(
                "fabricated-result-id",
                lambda text, record: None,
            )
        )
    assert clock_calls == []


@pytest.mark.fast
def test_trafilatura_text_is_transient_and_not_returned_in_source_record() -> None:
    page = FetchedPage(
        result_id="opaque-result-id",
        final_url="https://public.fixture.example/article",
        media_type="text/html",
        body=b"""
        <html><body><main>
        <h1>Quiet affection</h1>
        <p>Kindness can make an ordinary morning feel deeply cared for.</p>
        <p>Gentle attention turns small moments into warmth.</p>
        </main></body></html>
        """,
    )

    analysis_signals: dict[str, int] = {}
    analyzer_records: list[DiscoveryRecord] = []

    def analyze(text: str, record: DiscoveryRecord) -> None:
        analysis_signals["word_count"] = len(text.split())
        analyzer_records.append(record)
        assert record.result_id == page.result_id
        assert record.source_url == page.final_url
        assert record.retrieved_at == RETRIEVED_AT
        assert record.rights_evidence == RULES[0].rights_evidence
        with pytest.raises(FrozenInstanceError):
            setattr(record, "result_id", "changed")

    record = analyze_fetched_page(page, RETRIEVED_AT, RULES, analyze)

    assert analyzer_records == [record]
    assert record.result_id == "opaque-result-id"
    assert record.source_url == "https://public.fixture.example/article"
    assert record.retrieved_at == RETRIEVED_AT
    assert record.rights_evidence == RULES[0].rights_evidence
    assert analysis_signals["word_count"] >= 10
    assert not hasattr(record, "text")
    assert "ordinary morning" not in repr(record)


@pytest.mark.fast
def test_plain_text_uses_strict_bounded_decoding_and_normalized_lines() -> None:
    body = (
        b"\xef\xbb\xbfProject Gutenberg EBook of A Gentle Story\r\n"
        b"\r\n"
        b"CHAPTER I\r"
        b"The violet lanterns guarded the quiet harbor through the warm night.\r\n"
        b"*** END OF THE PROJECT GUTENBERG EBOOK A GENTLE STORY ***\r\n"
    )
    analyzed: list[bool] = []
    page = FetchedPage(
        result_id="plain-result-id",
        final_url="https://public.fixture.example/gutenberg.txt",
        media_type="text/plain",
        body=body,
    )

    def analyze(text: str, record: DiscoveryRecord) -> None:
        assert text == (
            "Project Gutenberg EBook of A Gentle Story\n"
            "\n"
            "CHAPTER I\n"
            "The violet lanterns guarded the quiet harbor through the warm night.\n"
            "*** END OF THE PROJECT GUTENBERG EBOOK A GENTLE STORY ***"
        )
        assert record.result_id == page.result_id
        assert record.source_url == page.final_url
        assert record.retrieved_at == RETRIEVED_AT
        assert record.rights_evidence == RULES[0].rights_evidence
        analyzed.append(True)

    record = analyze_fetched_page(page, RETRIEVED_AT, RULES, analyze)
    assert record.result_id == "plain-result-id"
    assert analyzed == [True]

    invalid_bodies = (
        b"\xff" * 50,
        b" \r\n\t" * 20,
        b"Project Gutenberg\x00 control-heavy passage " * 4,
        b"a" * 100_001,
    )
    for invalid_body in invalid_bodies:
        invalid_page = FetchedPage(
            result_id="invalid-plain-result-id",
            final_url="https://public.fixture.example/invalid.txt",
            media_type="text/plain",
            body=invalid_body,
        )
        with pytest.raises(DiscoveryExtractionError, match="extraction failed"):
            analyze_fetched_page(
                invalid_page,
                RETRIEVED_AT,
                RULES,
                lambda text, record: None,
            )


@pytest.mark.fast
def test_extraction_failure_creates_no_source_record() -> None:
    page = FetchedPage(
        result_id="opaque-result-id",
        final_url="https://public.fixture.example/empty",
        media_type="text/html",
        body=b"<html><body><script>ignored()</script></body></html>",
    )

    with pytest.raises(DiscoveryExtractionError, match="extraction failed"):
        analyze_fetched_page(
            page,
            RETRIEVED_AT,
            RULES,
            lambda text, record: None,
        )


@pytest.mark.fast
def test_redirect_to_non_curated_final_host_fails_closed() -> None:
    page = FetchedPage(
        result_id="opaque-result-id",
        final_url="https://unapproved.example/article",
        media_type="text/html",
        body=b"<html><body><p>This text must never be analyzed.</p></body></html>",
    )

    with pytest.raises(DiscoveryExtractionError, match="extraction failed"):
        analyze_fetched_page(
            page,
            RETRIEVED_AT,
            RULES,
            lambda text, record: None,
        )


@pytest.mark.fast
def test_extraction_rechecks_the_fetched_media_type() -> None:
    page = FetchedPage(
        result_id="opaque-result-id",
        final_url="https://public.fixture.example/article",
        media_type="application/json",
        body=b'{"content": "This must not be treated as a public page."}',
    )

    with pytest.raises(DiscoveryExtractionError, match="extraction failed"):
        analyze_fetched_page(
            page,
            RETRIEVED_AT,
            RULES,
            lambda text, record: None,
        )
