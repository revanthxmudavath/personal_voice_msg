"""Runnable entrypoint for the discovery container's default command --
see src/personal_voice_msg/discovery_worker_entrypoint.py for what this
actually does and does not do (verification harness, not the production
weekly pipeline)."""

from __future__ import annotations

import argparse
import asyncio

from personal_voice_msg.discovery.baseline import DeterministicDiscovery
from personal_voice_msg.discovery.web import DiscoveryWebSession, FetchPolicy
from personal_voice_msg.discovery_worker_entrypoint import run_discovery_worker


async def _main(searxng_base_url: str, budget_seconds: float) -> int:
    web_session = DiscoveryWebSession(FetchPolicy())
    discovery = DeterministicDiscovery(searxng_base_url, web_session)
    return await run_discovery_worker(
        discovery, web_session, wall_clock_budget_seconds=budget_seconds
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--searxng-base-url", required=True)
    parser.add_argument("--budget-seconds", type=float, default=60.0)
    args = parser.parse_args()
    analyzed_count = asyncio.run(_main(args.searxng_base_url, args.budget_seconds))
    print(f"analyzed {analyzed_count} pages")
