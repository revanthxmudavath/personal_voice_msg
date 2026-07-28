from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest

from personal_voice_msg.discovery.baseline import (
    CURATED_SOURCES,
    DISCOVERY_QUERIES,
    DeterministicDiscovery,
    DiscoveryExtractionError,
    DiscoveryRecord,
    DiscoverySearchError,
)
from personal_voice_msg.discovery.web import (
    DiscoveryBoundaryError,
    DiscoveryWebSession,
)

pytestmark = pytest.mark.live

if os.environ.get("T07_LIVE_DISCOVERY") != "1":
    pytestmark = [
        pytest.mark.live,
        pytest.mark.skip(reason="requires T07_LIVE_DISCOVERY=1"),
    ]

EXPECTED_HOSTS = (
    "standardebooks.org",
    "en.wikisource.org",
    "en.wikiquote.org",
)
REQUIRED_ENGINES = frozenset(
    {"startpage", "wikiquote", "wikisource"}
)
MAX_FETCH_ATTEMPTS = 5
QUALIFICATION_CYCLES = 3


def _wait_for_searxng(base_url: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    config_url = f"{base_url.rstrip('/')}/config"
    deadline = time.monotonic() + 30
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with opener.open(config_url, timeout=1) as response:
                payload = json.load(response)
            engines = payload.get("engines") if isinstance(payload, dict) else None
            if not isinstance(engines, list):
                raise RuntimeError("SearXNG configuration is invalid")
            enabled = {
                engine.get("name")
                for engine in engines
                if isinstance(engine, dict) and engine.get("enabled") is True
            }
            if not REQUIRED_ENGINES.issubset(enabled):
                raise RuntimeError("required public search engines are not enabled")
            return
        except OSError as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError("SearXNG did not become ready") from last_error


def _matches_hostname(hostname: str | None, expected: str) -> bool:
    return hostname == expected or (
        hostname is not None and hostname.endswith(f".{expected}")
    )


async def _qualify_query(
    searxng_url: str,
    query: str,
    expected_hostname: str,
) -> str | None:
    session = DiscoveryWebSession()
    discovery = DeterministicDiscovery(searxng_url, session)
    try:
        try:
            results = await discovery.search_web(query)
        except DiscoverySearchError:
            return None
        for result in results[:MAX_FETCH_ATTEMPTS]:
            signals: dict[str, int] = {}

            def analyze(text: str, provenance: DiscoveryRecord) -> None:
                signals["word_count"] = len(text.split())
                signals["provenance_present"] = int(
                    provenance.result_id == result.result_id
                )

            try:
                record = await discovery.analyze_result(result.result_id, analyze)
            except (DiscoveryBoundaryError, DiscoveryExtractionError):
                continue

            source_hostname = urlsplit(record.source_url).hostname
            rights_rule = next(
                (
                    rule
                    for rule in CURATED_SOURCES
                    if _matches_hostname(source_hostname, rule.hostname)
                ),
                None,
            )
            if not _matches_hostname(source_hostname, expected_hostname):
                continue
            assert signals.get("word_count", 0) >= 8
            assert signals.get("provenance_present") == 1
            assert record.retrieved_at.tzinfo is UTC
            assert rights_rule is not None
            assert record.rights_evidence == rights_rule.rights_evidence
            assert not hasattr(record, "text")
            assert not hasattr(record, "analysis")
            assert source_hostname is not None
            return source_hostname
    finally:
        session.close()
    return None


async def _run_three_cycles(searxng_url: str) -> None:
    successful_records = 0
    async with asyncio.timeout(480):
        for cycle in range(QUALIFICATION_CYCLES):
            cycle_hosts: list[str] = []
            for query, expected_hostname in zip(
                DISCOVERY_QUERIES,
                EXPECTED_HOSTS,
                strict=True,
            ):
                hostname = await _qualify_query(
                    searxng_url,
                    query,
                    expected_hostname,
                )
                if hostname is not None:
                    cycle_hosts.append(hostname)
            assert len(cycle_hosts) >= 2, (
                f"cycle {cycle + 1} produced fewer than two valid records"
            )
            assert len(set(cycle_hosts)) >= 2, (
                f"cycle {cycle + 1} produced fewer than two source domains"
            )
            assert EXPECTED_HOSTS[0] in cycle_hosts, (
                f"cycle {cycle + 1} produced no non-Wikimedia general-web record"
            )
            print(
                "T07_LIVE_CYCLE "
                + json.dumps(
                    {
                        "completed_at": datetime.now(UTC).isoformat(),
                        "cycle": cycle + 1,
                        "qualified_hosts": sorted(cycle_hosts),
                        "valid_record_count": len(cycle_hosts),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            successful_records += len(cycle_hosts)
            if cycle + 1 < QUALIFICATION_CYCLES:
                await asyncio.sleep(90)
    assert successful_records >= 6


def test_repeated_live_runs_produce_valid_curated_source_records() -> None:
    assert len(DISCOVERY_QUERIES) == len(EXPECTED_HOSTS)
    searxng_url = os.environ.get("SEARXNG_URL")
    if not searxng_url:
        pytest.fail("SEARXNG_URL is required when T07 live discovery is active")
    _wait_for_searxng(searxng_url)
    asyncio.run(_run_three_cycles(searxng_url))
