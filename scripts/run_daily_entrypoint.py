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
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.config import Settings, load_settings
from personal_voice_msg.daily_send_entrypoint import run_daily_entrypoint
from personal_voice_msg.database import (
    Database,
    MessageState,
    recipient_key_for_chat_id,
)
from personal_voice_msg.scheduling import PACIFIC

# States delivery.py's own docstring says must be "surfaced for the owner
# to check" -- with no alerting built yet, a non-zero cron exit is the
# only signal available.
_FAILURE_STATES = (MessageState.FAILED, MessageState.DELIVERY_UNKNOWN)


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


def _exit_code_for(result: MessageState | None) -> int:
    """FAILED and DELIVERY_UNKNOWN must not exit 0 -- every other outcome
    (SENT, QUEUED, RESERVED, ..., and None for "not due") is a normal
    tick."""
    return 1 if result in _FAILURE_STATES else 0


async def _run(settings: Settings, database_path: Path) -> None:
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
    exit_code = _exit_code_for(result)
    if exit_code != 0:
        raise SystemExit(exit_code)


def main() -> None:
    args = parse_args()
    # Loaded outside the try/except below: if this itself fails, there is
    # no Settings yet to redact with, and nothing secret has been loaded,
    # so the raw exception is safe to propagate as before.
    settings = load_settings(args.config)
    try:
        asyncio.run(_run(settings, args.database))
    except Exception as exc:
        # A real failure here (e.g. AudioPipelineError from a synthesis
        # failure) can embed secrets such as the voice embedding path --
        # config.py's Settings.redactor() knows what to scrub. Redact
        # before this reaches stderr, which under cron typically lands in
        # a mail spool or log file.
        print(settings.redactor().redact(str(exc)), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
