from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import secrets
import socket
import ssl
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp import ClientResponse
from aiohttp.abc import AbstractResolver, ResolveResult

MAX_SEARCH_RESULTS = 20
MAX_TITLE_CHARACTERS = 200
MAX_SNIPPET_CHARACTERS = 500
MAX_RAW_TITLE_CHARACTERS = 2_000
MAX_RAW_SNIPPET_CHARACTERS = 5_000
MAX_URL_CHARACTERS = 2_048
GENERIC_BOUNDARY_ERROR = "discovery web boundary rejected the request"
SUPPORTED_MEDIA_TYPES = frozenset(
    {"application/xhtml+xml", "text/html", "text/plain"}
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
LOCAL_HOST_SUFFIXES = (".home.arpa", ".internal", ".local", ".localhost")
DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
NUMERIC_HOST = re.compile(r"[0-9a-fx.:]+", re.IGNORECASE)
HTTP_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
NAT64_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class DiscoveryBoundaryError(RuntimeError):
    """Report one privacy-safe failure for every rejected web operation."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A raw result supplied only by the trusted search adapter added in T07."""

    url: str = field(repr=False)
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Bounded metadata safe to expose beside an opaque result capability."""

    result_id: str
    title: str
    snippet: str
    display_hostname: str


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """A bounded transport result; its URL and body stay out of representations."""

    result_id: str
    final_url: str = field(repr=False)
    media_type: str
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    max_body_bytes: int = 1_048_576
    max_redirects: int = 5
    total_timeout_seconds: float = 15.0
    dns_timeout_seconds: float = 3.0
    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 3.0
    max_headers: int = 64
    max_line_size: int = 8_192
    max_field_size: int = 8_192

    def __post_init__(self) -> None:
        values = (
            self.max_body_bytes,
            self.max_redirects,
            self.total_timeout_seconds,
            self.dns_timeout_seconds,
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.max_headers,
            self.max_line_size,
            self.max_field_size,
        )
        if any(value <= 0 for value in values):
            raise ValueError("fetch policy limits must be positive")


@dataclass(frozen=True, slots=True)
class _StoredResult:
    url: str = field(repr=False)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _boundary_error() -> DiscoveryBoundaryError:
    return DiscoveryBoundaryError(GENERIC_BOUNDARY_ERROR)


def _sanitize_metadata(value: str, limit: int, raw_limit: int) -> str:
    if not isinstance(value, str) or len(value) > raw_limit:
        raise _boundary_error()
    parser = _TextExtractor()
    try:
        parser.feed(html.unescape(value))
        parser.close()
    except (ValueError, RecursionError):
        raise _boundary_error() from None
    normalized = unicodedata.normalize("NFKC", "".join(parser.parts))
    safe = "".join(
        " "
        if character.isspace()
        else character
        if unicodedata.category(character) not in {"Cc", "Cf"}
        else ""
        for character in normalized
    )
    return " ".join(safe.split())[:limit]


def is_public_address(address: str) -> bool:
    """Return whether an address is native and globally routable."""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if (
        not parsed.is_global
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_private
        or parsed.is_reserved
        or parsed.is_unspecified
    ):
        return False
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return False
        if parsed.sixtofour is not None or parsed.teredo is not None:
            return False
        if any(parsed in prefix for prefix in NAT64_PREFIXES):
            return False
    return True


def _canonical_hostname(hostname: str) -> tuple[str, bool]:
    if "%" in hostname:
        raise _boundary_error()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if NUMERIC_HOST.fullmatch(hostname):
            raise _boundary_error() from None
        try:
            canonical = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise _boundary_error() from None
        if (
            "." not in canonical
            or len(canonical) > 253
            or canonical.endswith(LOCAL_HOST_SUFFIXES)
            or any(not DOMAIN_LABEL.fullmatch(label) for label in canonical.split("."))
        ):
            raise _boundary_error()
        return canonical, False
    if not is_public_address(str(address)):
        raise _boundary_error()
    return address.compressed, isinstance(address, ipaddress.IPv6Address)


def canonical_public_url(raw_url: str) -> str:
    """Validate and canonicalize a public HTTP(S) URL without resolving it."""

    if (
        not isinstance(raw_url, str)
        or not raw_url
        or len(raw_url) > MAX_URL_CHARACTERS
        or "\\" in raw_url
        or any(
            character.isspace()
            or ord(character) <= 31
            or ord(character) == 127
            for character in raw_url
        )
    ):
        raise _boundary_error()
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        raise _boundary_error() from None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _boundary_error()
    expected_port = 80 if scheme == "http" else 443
    if port is not None and port != expected_port:
        raise _boundary_error()
    hostname, is_ipv6 = _canonical_hostname(parsed.hostname)
    netloc = f"[{hostname}]" if is_ipv6 else hostname
    path = parsed.path or "/"
    canonical = SplitResult(scheme, netloc, path, parsed.query, "")
    rendered = urlunsplit(canonical)
    if len(rendered) > MAX_URL_CHARACTERS:
        raise _boundary_error()
    return rendered


class _PublicResolver(AbstractResolver):
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds
        self._answers: dict[tuple[str, int], frozenset[str]] = {}

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        query_host = f"{host.rstrip('.')}."
        loop = asyncio.get_running_loop()
        try:
            raw_answers = await asyncio.wait_for(
                loop.getaddrinfo(
                    query_host,
                    port,
                    family=family,
                    type=socket.SOCK_STREAM,
                ),
                timeout=self._timeout_seconds,
            )
        except (OSError, TimeoutError):
            raise OSError("destination resolution rejected") from None
        addresses = frozenset(str(answer[4][0]) for answer in raw_answers)
        if not addresses or any(
            not is_public_address(address) for address in addresses
        ):
            raise OSError("destination resolution rejected")
        key = (host.lower(), port)
        previous = self._answers.get(key)
        if previous is not None and previous != addresses:
            raise OSError("destination resolution changed")
        self._answers[key] = addresses
        results: list[ResolveResult] = []
        seen: set[tuple[int, str]] = set()
        for answer in raw_answers:
            answer_family, _, protocol, _, socket_address = answer
            address = str(socket_address[0])
            identity = (int(answer_family), address)
            if identity in seen:
                continue
            seen.add(identity)
            results.append(
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=port,
                    family=answer_family,
                    proto=protocol,
                    flags=socket.AI_NUMERICHOST,
                )
            )
        return results

    async def close(self) -> None:
        self._answers.clear()


def _public_socket_factory(address_info: aiohttp.AddrInfoType) -> socket.socket:
    family, socket_type, protocol, _, socket_address = address_info
    if not is_public_address(socket_address[0]):
        raise OSError("destination socket rejected")
    return socket.socket(family=family, type=socket_type, proto=protocol)


def _single_header(response: ClientResponse, name: str) -> str:
    values = response.headers.getall(name, [])
    if len(values) != 1:
        raise _boundary_error()
    value = values[0].strip()
    if not value or len(value) > MAX_URL_CHARACTERS:
        raise _boundary_error()
    return value


def _response_media_type(response: ClientResponse) -> str:
    raw = _single_header(response, "Content-Type")
    sections = [section.strip() for section in raw.split(";")]
    media_type = sections[0].lower()
    if (
        len(media_type.split("/")) != 2
        or any(not HTTP_TOKEN.fullmatch(token) for token in media_type.split("/"))
    ):
        raise _boundary_error()
    for parameter in sections[1:]:
        if "=" not in parameter:
            raise _boundary_error()
        name, value = (part.strip() for part in parameter.split("=", maxsplit=1))
        if not HTTP_TOKEN.fullmatch(name) or not value:
            raise _boundary_error()
        if value.startswith('"'):
            contains_control_character = any(
                ord(character) < 32 or ord(character) == 127 for character in value
            )
            if (
                len(value) < 2
                or not value.endswith('"')
                or contains_control_character
            ):
                raise _boundary_error()
        elif not HTTP_TOKEN.fullmatch(value):
            raise _boundary_error()
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise _boundary_error()
    return media_type


def _validate_response_framing(response: ClientResponse, policy: FetchPolicy) -> None:
    encodings = response.headers.getall("Content-Encoding", [])
    if encodings and (
        len(encodings) != 1 or encodings[0].strip().lower() != "identity"
    ):
        raise _boundary_error()
    lengths = response.headers.getall("Content-Length", [])
    if len(lengths) > 1:
        raise _boundary_error()
    if lengths:
        try:
            length = int(lengths[0], 10)
        except ValueError:
            raise _boundary_error() from None
        if length < 0 or length > policy.max_body_bytes:
            raise _boundary_error()


async def _read_bounded_body(
    response: ClientResponse,
    policy: FetchPolicy,
) -> bytes:
    chunks: list[bytes] = []
    length = 0
    async for chunk in response.content.iter_chunked(65_536):
        length += len(chunk)
        if length > policy.max_body_bytes:
            raise _boundary_error()
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        raise _boundary_error()
    return body


async def _fetch_url(
    url: str,
    result_id: str,
    policy: FetchPolicy,
) -> FetchedPage:
    resolver = _PublicResolver(policy.dns_timeout_seconds)
    tls_context = ssl.create_default_context()
    connector = aiohttp.TCPConnector(
        family=socket.AF_UNSPEC,
        force_close=True,
        limit=1,
        limit_per_host=1,
        resolver=resolver,
        socket_factory=_public_socket_factory,
        ssl=tls_context,
        use_dns_cache=False,
    )
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=policy.connect_timeout_seconds,
        sock_connect=policy.connect_timeout_seconds,
        sock_read=policy.read_timeout_seconds,
    )
    current_url = url
    visited = {current_url}
    redirects = 0
    try:
        async with (
            asyncio.timeout(policy.total_timeout_seconds),
            aiohttp.ClientSession(
                auto_decompress=False,
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
                max_field_size=policy.max_field_size,
                max_headers=policy.max_headers,
                max_line_size=policy.max_line_size,
                timeout=timeout,
                trust_env=False,
            ) as client,
        ):
            while True:
                parsed = urlsplit(current_url)
                response = await client.get(
                    current_url,
                    allow_redirects=False,
                    headers={
                        "Accept": "text/html, application/xhtml+xml, text/plain",
                        "Accept-Encoding": "identity",
                        "User-Agent": "personal-voice-msg/0.1",
                    },
                    server_hostname=(
                        parsed.hostname if parsed.scheme == "https" else None
                    ),
                )
                async with response:
                    if response.status in REDIRECT_STATUSES:
                        if redirects >= policy.max_redirects:
                            raise _boundary_error()
                        location = _single_header(response, "Location")
                        redirected = canonical_public_url(
                            urljoin(current_url, location)
                        )
                        if (
                            parsed.scheme == "https"
                            and urlsplit(redirected).scheme != "https"
                        ):
                            raise _boundary_error()
                        if redirected in visited:
                            raise _boundary_error()
                        visited.add(redirected)
                        redirects += 1
                        current_url = redirected
                        continue
                    if response.status != 200:
                        raise _boundary_error()
                    media_type = _response_media_type(response)
                    _validate_response_framing(response, policy)
                    body = await _read_bounded_body(response, policy)
                    return FetchedPage(
                        result_id=result_id,
                        final_url=current_url,
                        media_type=media_type,
                        body=body,
                    )
    except DiscoveryBoundaryError:
        raise
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
        raise _boundary_error() from None


class DiscoveryWebSession:
    """Own the private result capabilities for exactly one discovery run."""

    def __init__(self, policy: FetchPolicy | None = None) -> None:
        self._policy = policy or FetchPolicy()
        self._results: dict[str, _StoredResult] = {}
        self._closed = False
        self._fetch_in_progress = False

    def record_search_results(
        self,
        hits: tuple[SearchHit, ...],
    ) -> tuple[SearchResult, ...]:
        if self._closed or len(self._results) + len(hits) > MAX_SEARCH_RESULTS:
            raise _boundary_error()
        prepared: list[tuple[str, str, str, str]] = []
        for hit in hits:
            if not isinstance(hit, SearchHit):
                raise _boundary_error()
            url = canonical_public_url(hit.url)
            hostname = urlsplit(url).hostname
            if hostname is None:
                raise _boundary_error()
            prepared.append(
                (
                    url,
                    _sanitize_metadata(
                        hit.title,
                        MAX_TITLE_CHARACTERS,
                        MAX_RAW_TITLE_CHARACTERS,
                    ),
                    _sanitize_metadata(
                        hit.snippet,
                        MAX_SNIPPET_CHARACTERS,
                        MAX_RAW_SNIPPET_CHARACTERS,
                    ),
                    hostname,
                )
            )
        issued: list[SearchResult] = []
        for url, title, snippet, hostname in prepared:
            result_id = secrets.token_urlsafe(24)
            while result_id in self._results:
                result_id = secrets.token_urlsafe(24)
            self._results[result_id] = _StoredResult(url)
            issued.append(SearchResult(result_id, title, snippet, hostname))
        return tuple(issued)

    async def fetch_public_page(self, result_id: str) -> FetchedPage:
        if (
            self._closed
            or self._fetch_in_progress
            or not isinstance(result_id, str)
        ):
            raise _boundary_error()
        stored = self._results.pop(result_id, None)
        if stored is None:
            raise _boundary_error()
        self._fetch_in_progress = True
        try:
            return await _fetch_url(stored.url, result_id, self._policy)
        finally:
            self._fetch_in_progress = False

    def close(self) -> None:
        if self._fetch_in_progress:
            raise _boundary_error()
        self._closed = True
        self._results.clear()
