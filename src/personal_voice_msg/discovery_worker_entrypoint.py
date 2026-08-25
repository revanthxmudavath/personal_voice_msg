"""Bounded verification-harness entrypoint for the discovery container.

Reuses T07's already-tested DeterministicDiscovery/DiscoveryWebSession to
give the discovery container a real, bounded, resource-exhaustible process
to run and fault-inject against. Deliberately stops at DiscoveryRecord --
it does not build InspirationCards, does not call generation or judging,
and does not touch the database. Wiring the full weekly production
pipeline (search -> card -> generate -> judge -> queue) is a pre-existing
gap this entrypoint does not solve -- see
docs/superpowers/specs/2026-08-24-t18-cloud-container-hardening-design.md.
"""

from __future__ import annotations

import time

from personal_voice_msg.discovery.baseline import (
    DISCOVERY_QUERIES,
    DeterministicDiscovery,
    DiscoveryExtractionError,
    DiscoverySearchError,
)
from personal_voice_msg.discovery.web import DiscoveryBoundaryError, DiscoveryWebSession


def _null_analyzer(text: str, record: object) -> None:
    """Accept every extracted page -- this harness counts successful
    extractions, it does not apply T08's content/rights transformation."""


async def run_discovery_worker(
    discovery: DeterministicDiscovery,
    web_session: DiscoveryWebSession,
    *,
    wall_clock_budget_seconds: float = 60.0,
) -> int:
    started = time.monotonic()
    analyzed = 0
    for query in DISCOVERY_QUERIES:
        if time.monotonic() - started >= wall_clock_budget_seconds:
            break
        try:
            results = await discovery.search_web(query)
        except DiscoverySearchError:
            continue
        for result in results:
            if time.monotonic() - started >= wall_clock_budget_seconds:
                break
            try:
                await discovery.analyze_result(result.result_id, _null_analyzer)
                analyzed += 1
            except (DiscoveryBoundaryError, DiscoveryExtractionError):
                continue
    return analyzed
