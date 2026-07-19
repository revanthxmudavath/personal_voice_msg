from __future__ import annotations

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

NEAR_DUPLICATE_THRESHOLD = 84.0


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


class MessageHistory:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, text: str, now: datetime) -> int:
        return self.database.create_message(text, now)

    def evaluate(
        self,
        candidate: str,
        *,
        source_text: str | None = None,
    ) -> DuplicateDecision:
        normalized_candidate = normalize_text(candidate)
        if not normalized_candidate:
            raise ValueError("candidate text must not be empty")
        if source_text is not None and copies_source_span(candidate, source_text):
            return DuplicateDecision(False, DuplicateReason.SOURCE_COPY, None)

        connection = self.database.connect()
        try:
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
        finally:
            connection.close()

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
