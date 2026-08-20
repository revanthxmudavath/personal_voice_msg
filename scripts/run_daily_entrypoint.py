"""Runnable entrypoint for the daily-send process: run one tick, do
whatever's due (poll for a STOP, then advance today's delivery), and
exit. An external timer (cron inside the container, or a systemd timer --
T18's concern, not this script's) invokes this every 1-2 minutes; nothing
here loops or sleeps.

usage: run_daily_entrypoint.py --config CONFIG --database DATABASE
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.config import load_settings
from personal_voice_msg.daily_send_entrypoint import run_daily_entrypoint
from personal_voice_msg.database import Database, recipient_key_for_chat_id
from personal_voice_msg.scheduling import PACIFIC


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one tick of the daily-send entrypoint: poll for a STOP "
            "command, then advance today's delivery by one step if the "
            "daily-send window is currently open."
        )
    )
    parser.add_argument(
        "--config", type=Path, required=True,
        help="path to a load_settings-compatible TOML configuration file",
    )
    parser.add_argument(
        "--database", type=Path, required=True,
        help="path to the SQLite state file",
    )
    return parser.parse_args()


async def _run(config_path: Path, database_path: Path) -> None:
    settings = load_settings(config_path)
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
    embedding_path = settings.voice_embedding.reveal()
    database = Database(database_path)
    database.migrate()
    now = datetime.now(UTC)
    pacific_date = now.astimezone(PACIFIC).date()

    async with aiohttp.ClientSession() as session:
        result = await run_daily_entrypoint(
            database, settings, session, recipient_key, pacific_date,
            embedding_path, now,
        )

    print("not due, skipped" if result is None else result.value)


def main() -> None:
    args = parse_args()
    asyncio.run(_run(args.config, args.database))


if __name__ == "__main__":
    main()
