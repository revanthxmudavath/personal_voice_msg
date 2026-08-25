from __future__ import annotations

import asyncio
import os
import time

import pytest

from personal_voice_msg.discovery.baseline import DeterministicDiscovery
from personal_voice_msg.discovery.web import DiscoveryWebSession, FetchPolicy
from personal_voice_msg.discovery_worker_entrypoint import run_discovery_worker

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("T07_NETWORK_HARNESS") != "1",
    reason="requires the isolated T07 Docker network",
)
def test_run_discovery_worker_returns_a_bounded_record_count() -> None:
    """Reuses T07's own real-network test harness (T07_NETWORK_HARNESS=1,
    SEARXNG_URL) rather than inventing a second one -- see
    tests/integration/test_discovery_baseline_network.py for the fixture
    this depends on."""

    async def _run() -> int:
        web_session = DiscoveryWebSession(FetchPolicy())
        discovery = DeterministicDiscovery(
            os.environ["SEARXNG_URL"], web_session
        )
        return await run_discovery_worker(
            discovery, web_session, wall_clock_budget_seconds=45.0
        )

    count = asyncio.run(_run())
    assert count >= 1, (
        "expected at least one real page analyzed from the real SearXNG fixture"
    )


def test_run_discovery_worker_stops_at_the_wall_clock_budget() -> None:
    """No real SearXNG needed for this one: an unreachable endpoint proves
    the budget loop still terminates promptly rather than hanging on
    DISCOVERY_QUERIES's fixed 3-query loop with retries."""

    async def _run() -> tuple[int, float]:
        web_session = DiscoveryWebSession(FetchPolicy())
        discovery = DeterministicDiscovery("http://127.0.0.1:1", web_session)
        started = time.monotonic()
        count = await run_discovery_worker(
            discovery, web_session, wall_clock_budget_seconds=5.0
        )
        return count, time.monotonic() - started

    count, elapsed = asyncio.run(_run())
    assert count == 0
    assert elapsed < 15.0
