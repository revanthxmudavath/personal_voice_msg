from __future__ import annotations

import asyncio
import os
import time
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone

import pytest

from personal_voice_msg.discovery.baseline import (
    DISCOVERY_QUERIES,
    DeterministicDiscovery,
    SourceRule,
)
from personal_voice_msg.discovery.web import DiscoveryWebSession

pytestmark = pytest.mark.integration

if os.environ.get("T07_NETWORK_HARNESS") != "1":
    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skip(reason="requires the isolated T07 Docker network"),
    ]

RULES = (
    SourceRule(
        hostname="public.fixture.example",
        rights_evidence="Public fixture content created for integration testing.",
    ),
)
FIXED_FETCH_TIME = datetime(
    2026,
    7,
    27,
    5,
    0,
    tzinfo=timezone(-timedelta(hours=7)),
)


def _wait_for_searxng() -> str:
    base_url = os.environ["SEARXNG_URL"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 30
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with opener.open(base_url, timeout=1) as response:
                if response.status == 200:
                    return base_url
        except OSError as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError("SearXNG did not become ready") from last_error


@pytest.mark.parametrize("query", DISCOVERY_QUERIES)
def test_real_searxng_result_flows_through_result_id_to_trafilatura(
    query: str,
) -> None:
    async def exercise() -> None:
        web_session = DiscoveryWebSession()
        clock_calls: list[None] = []

        def fixed_clock() -> datetime:
            clock_calls.append(None)
            return FIXED_FETCH_TIME

        discovery = DeterministicDiscovery(
            _wait_for_searxng(),
            web_session,
            RULES,
            clock=fixed_clock,
        )

        results = await discovery.search_web(query)

        assert len(results) == 1
        assert results[0].title == "Quiet affection"
        assert results[0].display_hostname == "public.fixture.example"
        assert not hasattr(results[0], "url")

        analysis_signals: dict[str, bool | int] = {}

        def analyze(text: str) -> None:
            analysis_signals["word_count"] = len(text.split())
            analysis_signals["mentions_kindness"] = "Kindness" in text

        record = await discovery.analyze_result(results[0].result_id, analyze)

        assert clock_calls == [None]
        assert record.result_id == results[0].result_id
        assert record.source_url == "http://public.fixture.example/article"
        assert record.retrieved_at == datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        assert analysis_signals["word_count"] >= 10
        assert analysis_signals["mentions_kindness"] is True
        assert record.rights_evidence == RULES[0].rights_evidence
        assert "ordinary morning" not in repr(record)
        assert "text" not in asdict(record)
        assert "analysis" not in asdict(record)

    asyncio.run(exercise())
