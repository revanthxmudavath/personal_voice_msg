from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rapidfuzz import fuzz

from personal_voice_msg.database import Database
from personal_voice_msg.normalization import (
    copies_source_span,
    normalize_text,
    normalized_hash,
)

NEAR_DUPLICATE_THRESHOLD = 79.0


class DuplicateReason(StrEnum):
    EXACT = "exact"
    NEAR = "near"
    SOURCE_COPY = "source_copy"


@dataclass(frozen=True, slots=True)
class DuplicateDecision:
    accepted: bool
    reason: DuplicateReason | None
    matched_message_id: int | None
    score: float | None = None
    recorded_message_id: int | None = None


class MessageHistory:
    def __init__(self, database: Database) -> None:
        self.database = database

    def evaluate(
        self,
        candidate: str,
        *,
        source_text: str | None = None,
    ) -> DuplicateDecision:
        connection = self.database.connect()
        try:
            return self._evaluate_with_connection(
                connection,
                candidate,
                source_text=source_text,
            )
        finally:
            connection.close()

    def evaluate_and_record(
        self,
        candidate: str,
        now: datetime,
        *,
        source_text: str | None = None,
    ) -> DuplicateDecision:
        with self.database.write_transaction() as connection:
            decision = self._evaluate_with_connection(
                connection,
                candidate,
                source_text=source_text,
            )
            if not decision.accepted:
                return decision
            message_id = self.database.create_message_in_transaction(
                connection,
                candidate,
                now,
            )
            return DuplicateDecision(
                accepted=True,
                reason=None,
                matched_message_id=None,
                score=decision.score,
                recorded_message_id=message_id,
            )

    def _evaluate_with_connection(
        self,
        connection: sqlite3.Connection,
        candidate: str,
        *,
        source_text: str | None,
    ) -> DuplicateDecision:
        normalized_candidate = normalize_text(candidate)
        if not normalized_candidate:
            raise ValueError("candidate text must not be empty")
        if source_text is not None and copies_source_span(candidate, source_text):
            return DuplicateDecision(False, DuplicateReason.SOURCE_COPY, None)

        exact = connection.execute(
            """
            SELECT message_id FROM message_history
            WHERE normalized_hash = ?
            ORDER BY message_id
            LIMIT 1
            """,
            (normalized_hash(candidate),),
        ).fetchone()
        if exact is not None:
            return DuplicateDecision(
                False,
                DuplicateReason.EXACT,
                int(exact[0]),
                100.0,
            )

        rows = connection.execute(
            """
            SELECT id, text FROM messages
            ORDER BY id
            """,
        ).fetchall()

        best_message_id: int | None = None
        best_score = 0.0
        for row in rows:
            score = float(
                fuzz.token_sort_ratio(
                    normalized_candidate,
                    normalize_text(str(row[1])),
                    processor=None,
                )
            )
            if score > best_score:
                best_message_id = int(row[0])
                best_score = score

        if best_message_id is not None and best_score >= NEAR_DUPLICATE_THRESHOLD:
            return DuplicateDecision(
                False,
                DuplicateReason.NEAR,
                best_message_id,
                best_score,
            )
        return DuplicateDecision(True, None, None, best_score or None)
