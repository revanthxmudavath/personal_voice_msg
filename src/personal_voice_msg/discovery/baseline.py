from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

import aiohttp
from trafilatura import extract

from personal_voice_msg.discovery.web import (
    MAX_RAW_SNIPPET_CHARACTERS,
    MAX_RAW_TITLE_CHARACTERS,
    MAX_SEARCH_RESULTS,
    SUPPORTED_MEDIA_TYPES,
    DiscoveryWebSession,
    FetchedPage,
    SearchHit,
    SearchResult,
    canonical_public_url,
)

MAX_SEARCH_RESPONSE_BYTES = 1_048_576
MAX_SEARCH_JSON_DEPTH = 64
MAX_EXTRACTED_CHARACTERS = 100_000
MIN_EXTRACTED_CHARACTERS = 40
SEARCH_TIMEOUT_SECONDS = 10.0
DISCOVERY_QUERIES = (
    "!sp standardebooks.org ebooks love romance",
    "!ws historic love poem song lyrics tenderness",
    "!wq love affection kindness tenderness quotations",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DiscoverySearchError(RuntimeError):
    """Report a bounded, non-leaking SearXNG search failure."""


class DiscoveryExtractionError(RuntimeError):
    """Report a page that cannot produce a bounded transient extraction."""


@dataclass(frozen=True, slots=True)
class SourceRule:
    hostname: str
    rights_evidence: str

    def __post_init__(self) -> None:
        try:
            canonical = canonical_public_url(f"https://{self.hostname}/")
        except RuntimeError:
            canonical = ""
        canonical_hostname = urlsplit(canonical).hostname
        if canonical_hostname != self.hostname or not (
            1 <= len(self.rights_evidence.strip()) <= 1_000
        ):
            raise ValueError("source rule is invalid")


CURATED_SOURCES = (
    SourceRule(
        hostname="standardebooks.org",
        rights_evidence=(
            "Standard Ebooks states that its editions are believed public domain "
            "in the United States and its contributions are CC0; item-specific "
            "rights remain unknown until T08 validates the page evidence."
        ),
    ),
    SourceRule(
        hostname="en.wikisource.org",
        rights_evidence=(
            "Wikisource content may be public domain or freely licensed; "
            "item-specific rights remain unknown until T08 validates the page "
            "evidence."
        ),
    ),
    SourceRule(
        hostname="en.wikiquote.org",
        rights_evidence=(
            "Wikiquote platform licensing does not establish rights in underlying "
            "quotations; item-specific rights remain unknown."
        ),
    ),
)

@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    result_id: str
    source_url: str
    retrieved_at: datetime
    rights_evidence: str


def _capture_retrieval_time(clock: Callable[[], object]) -> datetime:
    try:
        retrieved_at = clock()
        if (
            not isinstance(retrieved_at, datetime)
            or retrieved_at.tzinfo is None
            or retrieved_at.utcoffset() is None
        ):
            raise ValueError
        return retrieved_at.astimezone(UTC)
    except MemoryError:
        raise
    except Exception:
        raise DiscoveryExtractionError("page extraction failed") from None


def _matching_rule(url: str, rules: Sequence[SourceRule]) -> SourceRule | None:
    try:
        canonical = canonical_public_url(url)
    except RuntimeError:
        return None
    hostname = urlsplit(canonical).hostname
    if hostname is None:
        return None
    for rule in rules:
        if hostname == rule.hostname or hostname.endswith(f".{rule.hostname}"):
            return rule
    return None


def _decode_plain_text(body: bytes) -> str | None:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _json_structure_is_bounded(text: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            depth += 1
            if depth > MAX_SEARCH_JSON_DEPTH:
                return False
        elif character in "}]":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _analyzer_accepts(
    analyzer: Callable[[str, DiscoveryRecord], None],
    text: str,
    record: DiscoveryRecord,
) -> bool:
    try:
        analyzer_result = cast(
            Callable[[str, DiscoveryRecord], object],
            analyzer,
        )(text, record)
    except MemoryError:
        raise
    except Exception:
        return False
    return analyzer_result is None


def _decode_json(body: bytes) -> tuple[bool, object | None]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False, None
    if not _json_structure_is_bounded(text):
        return False, None
    try:
        decoded: object = json.loads(text)
    except (ValueError, RecursionError):
        return False, None
    return True, decoded


def parse_searxng_response(
    payload: object,
    rules: Sequence[SourceRule] = CURATED_SOURCES,
) -> tuple[SearchHit, ...]:
    """Convert one trusted SearXNG response into bounded curated search hits."""

    if not isinstance(payload, Mapping):
        raise DiscoverySearchError("search response rejected")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise DiscoverySearchError("search response rejected")

    hits: list[SearchHit] = []
    seen_urls: set[str] = set()
    for raw_result in raw_results[: MAX_SEARCH_RESULTS * 5]:
        if not isinstance(raw_result, Mapping):
            continue
        url = raw_result.get("url")
        title = raw_result.get("title")
        content = raw_result.get("content", "")
        if content is None:
            content = ""
        if (
            not isinstance(url, str)
            or not isinstance(title, str)
            or not isinstance(content, str)
            or not title.strip()
            or len(title) > MAX_RAW_TITLE_CHARACTERS
            or len(content) > MAX_RAW_SNIPPET_CHARACTERS
        ):
            continue
        assert isinstance(url, str)
        assert isinstance(title, str)
        assert isinstance(content, str)
        rule = _matching_rule(url, rules)
        if rule is None:
            continue
        canonical = canonical_public_url(url)
        if canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        hits.append(SearchHit(url=canonical, title=title, snippet=content))
        if len(hits) == MAX_SEARCH_RESULTS:
            break
    return tuple(hits)


def analyze_fetched_page(
    page: FetchedPage,
    retrieved_at: datetime,
    rules: Sequence[SourceRule],
    analyzer: Callable[[str, DiscoveryRecord], None],
) -> DiscoveryRecord:
    """Analyze extracted text transiently without returning or persisting it."""

    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise ValueError("retrieval time must be timezone-aware")
    rule = _matching_rule(page.final_url, rules)
    if rule is None or page.media_type not in SUPPORTED_MEDIA_TYPES:
        raise DiscoveryExtractionError("page extraction failed")

    if page.media_type == "text/plain":
        decoded = _decode_plain_text(page.body)
        if decoded is None:
            raise DiscoveryExtractionError("page extraction failed")
        text = decoded
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if any(
            unicodedata.category(character) == "Cc"
            and character not in {"\n", "\t"}
            for character in text
        ):
            raise DiscoveryExtractionError("page extraction failed")
    else:
        try:
            extracted = extract(
                page.body,
                url=page.final_url,
                output_format="txt",
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except Exception:
            raise DiscoveryExtractionError("page extraction failed") from None
        if extracted is None:
            raise DiscoveryExtractionError("page extraction failed")
        text = extracted
    text = text.strip()
    if not MIN_EXTRACTED_CHARACTERS <= len(text) <= MAX_EXTRACTED_CHARACTERS:
        raise DiscoveryExtractionError("page extraction failed")

    record = DiscoveryRecord(
        result_id=page.result_id,
        source_url=page.final_url,
        retrieved_at=retrieved_at.astimezone(UTC),
        rights_evidence=rule.rights_evidence,
    )
    try:
        analyzer_accepted = _analyzer_accepts(analyzer, text, record)
    finally:
        del text
    if not analyzer_accepted:
        raise DiscoveryExtractionError("page extraction failed")
    return record


class DeterministicDiscovery:
    """Run fixed SearXNG queries behind the T06 result-ID fetch boundary."""

    def __init__(
        self,
        searxng_base_url: str,
        web_session: DiscoveryWebSession,
        source_rules: Sequence[SourceRule] = CURATED_SOURCES,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        try:
            parsed = urlsplit(searxng_base_url)
            parsed.port
        except ValueError:
            raise ValueError("trusted SearXNG endpoint is invalid") from None
        if (
            not searxng_base_url
            or len(searxng_base_url) > 2_048
            or "\\" in searxng_base_url
            or any(character.isspace() for character in searxng_base_url)
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not source_rules
        ):
            raise ValueError("trusted SearXNG endpoint is invalid")
        self._search_endpoint = f"{searxng_base_url.rstrip('/')}/search"
        self._web_session = web_session
        self._source_rules = tuple(source_rules)
        self._clock = clock

    async def search_web(self, query: str) -> tuple[SearchResult, ...]:
        if query not in DISCOVERY_QUERIES:
            raise DiscoverySearchError("search query rejected")
        timeout = aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(
                auto_decompress=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=timeout,
                trust_env=False,
            ) as client:
                async with client.get(
                    self._search_endpoint,
                    allow_redirects=False,
                    params={
                        "q": query,
                        "format": "json",
                        "categories": "general",
                        "language": "en",
                        "pageno": "1",
                        "safesearch": "2",
                    },
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "User-Agent": "personal-voice-msg/0.1",
                    },
                ) as response:
                    if response.status != 200:
                        raise DiscoverySearchError("search response rejected")
                    media_type = response.headers.get("Content-Type", "").split(
                        ";", maxsplit=1
                    )[0]
                    if media_type.lower() != "application/json":
                        raise DiscoverySearchError("search response rejected")
                    body = await _read_bounded_response(response)
        except DiscoverySearchError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise DiscoverySearchError("search response rejected") from None

        decoded, payload = _decode_json(body)
        if not decoded:
            raise DiscoverySearchError("search response rejected")
        hits = parse_searxng_response(payload, self._source_rules)
        return self._web_session.record_search_results(hits)

    async def analyze_result(
        self,
        result_id: str,
        analyzer: Callable[[str, DiscoveryRecord], None],
    ) -> DiscoveryRecord:
        page = await self._web_session.fetch_public_page(result_id)
        retrieved_at = _capture_retrieval_time(self._clock)
        return analyze_fetched_page(
            page,
            retrieved_at,
            self._source_rules,
            analyzer,
        )


async def _read_bounded_response(response: aiohttp.ClientResponse) -> bytes:
    chunks: list[bytes] = []
    length = 0
    async for chunk in response.content.iter_chunked(65_536):
        length += len(chunk)
        if length > MAX_SEARCH_RESPONSE_BYTES:
            raise DiscoverySearchError("search response rejected")
        chunks.append(chunk)
    if not chunks:
        raise DiscoverySearchError("search response rejected")
    return b"".join(chunks)
