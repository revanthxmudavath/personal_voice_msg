"""Promptfoo custom Python provider for the T10 100-trial qualification run.

For each corpus row (`context["vars"]` holding `theme`/`emotion`/`imagery`/
`tone` string values from `evals/t10/corpus.yaml`), this provider:

1. Builds the matching `InspirationCard` (fixed placeholder `source`,
   `evidence`, and `discovery_timestamp` -- generation only ever reads
   `.theme`/.emotion`/`.imagery`/`.tone`, per T10's Global Constraints).
2. Creates a fresh temporary SQLite file (never a production or shared
   database) and migrates it, so each trial's duplicate/history check runs
   against clean state.
3. Calls the real `generate_sentence()` boundary from Task 3 against the
   real Gemini API, using the real key loaded via `load_gemini_settings()`.
4. Returns `{"output": <validated sentence>}` on success, or
   `{"output": <clearly-labeled failure string>}` on a validation/client
   failure -- never raises uncaught, per Promptfoo's Python provider
   protocol (errors are reported in the response, not via exceptions).

Reads the real Gemini key via `GEMINI_GENERATION_CONFIG` (an env var
holding the path to a real `generation-settings.toml`) -- never reads or
logs the key file's contents itself; that happens inside
`load_gemini_settings()`.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

from personal_voice_msg.database import Database
from personal_voice_msg.discovery.inspiration import (
    Emotion,
    Imagery,
    InspirationCard,
    RightsCategory,
    Theme,
    Tone,
)
from personal_voice_msg.generation.config import load_gemini_settings
from personal_voice_msg.generation.gemini_client import GeminiClientError
from personal_voice_msg.generation.sentence import (
    SentenceValidationError,
    generate_sentence,
)
from personal_voice_msg.history import MessageHistory

# Fixed, non-secret placeholder provenance. Generation never reads these
# fields (only .theme/.emotion/.imagery/.tone reach the prompt), so a
# constant, already-validated placeholder is correct here, not a stand-in
# for real discovery provenance.
_PLACEHOLDER_SOURCE = "https://example.invalid/t10-qualification"
_PLACEHOLDER_EVIDENCE = "t10 qualification placeholder, unused by generation"
_PLACEHOLDER_DISCOVERY_TIMESTAMP = datetime(2026, 8, 4, tzinfo=UTC)

FAILURE_PREFIX = "T10_QUALIFICATION_FAILURE"


def _build_card(row_vars: dict[str, Any]) -> InspirationCard:
    return InspirationCard(
        theme=Theme(row_vars["theme"]),
        emotion=Emotion(row_vars["emotion"]),
        imagery=Imagery(row_vars["imagery"]),
        tone=Tone(row_vars["tone"]),
        source=_PLACEHOLDER_SOURCE,
        rights_category=RightsCategory.UNKNOWN,
        evidence=_PLACEHOLDER_EVIDENCE,
        discovery_timestamp=_PLACEHOLDER_DISCOVERY_TIMESTAMP,
    )


async def _run_trial(card: InspirationCard) -> str:
    settings = load_gemini_settings(Path(os.environ["GEMINI_GENERATION_CONFIG"]))

    temp_fd, temp_path_str = tempfile.mkstemp(
        prefix="t10-qualification-", suffix=".sqlite3"
    )
    os.close(temp_fd)
    temp_path = Path(temp_path_str)
    try:
        database = Database(temp_path)
        database.migrate()
        history = MessageHistory(database)

        async with aiohttp.ClientSession() as session:
            decision = await generate_sentence(
                session,
                settings.api_key,
                card,
                history,
                datetime.now(UTC),
            )

        if not decision.accepted:
            reason = (
                decision.reason.value if decision.reason is not None else "unknown"
            )
            return f"{FAILURE_PREFIX}: rejected by history check ({reason})"

        if decision.recorded_message_id is None:
            return (
                f"{FAILURE_PREFIX}: accepted decision carried no recorded_message_id"
            )

        return _fetch_sentence(database, decision.recorded_message_id)
    finally:
        # Clean up only after every read of the temp database is done --
        # never a production or shared database, and never left behind.
        temp_path.unlink(missing_ok=True)


def _fetch_sentence(database: Database, message_id: int) -> str:
    connection = database._connect()
    try:
        row = connection.execute(
            "SELECT text FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return f"{FAILURE_PREFIX}: recorded message vanished before it could be read"
    return str(row[0])


def call_api(
    prompt: str, options: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    row_vars = context.get("vars", {})
    try:
        card = _build_card(row_vars)
        sentence = asyncio.run(_run_trial(card))
        return {"output": sentence}
    except (SentenceValidationError, GeminiClientError) as error:
        return {
            "output": f"{FAILURE_PREFIX}: {type(error).__name__}",
        }
    except Exception as error:  # noqa: BLE001 - Promptfoo expects a response, never a raise.
        return {
            "output": f"{FAILURE_PREFIX}: unexpected {type(error).__name__}",
            "error": f"unexpected {type(error).__name__}",
        }
