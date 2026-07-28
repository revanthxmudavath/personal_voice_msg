from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from personal_voice_msg.discovery.baseline import (
    MAX_EXTRACTED_CHARACTERS,
    MIN_EXTRACTED_CHARACTERS,
    DiscoveryRecord,
)
from personal_voice_msg.discovery.web import (
    MAX_URL_CHARACTERS,
    canonical_public_url,
)
from personal_voice_msg.normalization import copies_source_span

MAX_RESULT_ID_CHARACTERS = 256
MAX_RIGHTS_EVIDENCE_CHARACTERS = 1_000
RESULT_ID = re.compile(r"[A-Za-z0-9_-]+")
_DRAFT_KEYS = frozenset({"theme", "emotion", "imagery", "tone"})


class InspirationCardValidationError(RuntimeError):
    """Report an invalid untrusted card without including its values."""


class Theme(StrEnum):
    APPRECIATION = "appreciation"
    AFFECTION = "affection"
    COMPANIONSHIP = "companionship"
    ENCOURAGEMENT = "encouragement"


class Emotion(StrEnum):
    GRATITUDE = "gratitude"
    JOY = "joy"
    WARMTH = "warmth"
    CALM = "calm"


class Imagery(StrEnum):
    MORNING_LIGHT = "morning_light"
    QUIET_GARDEN = "quiet_garden"
    WARM_HOME = "warm_home"
    OPEN_SKY = "open_sky"


class Tone(StrEnum):
    GENTLE = "gentle"
    PLAYFUL = "playful"
    TENDER = "tender"
    REASSURING = "reassuring"


class RightsCategory(StrEnum):
    UNKNOWN = "unknown"


def _rejected() -> InspirationCardValidationError:
    return InspirationCardValidationError("inspiration card rejected")


def _is_bounded_single_line(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and value == value.strip()
        and 1 <= len(value) <= maximum
        and not any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    )


def _canonical_source(value: object) -> str | None:
    if not _is_bounded_single_line(value, MAX_URL_CHARACTERS):
        return None
    assert isinstance(value, str)
    try:
        canonical = canonical_public_url(value)
    except RuntimeError:
        return None
    return canonical if canonical == value else None


def _utc_timestamp(value: object) -> datetime | None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        return None
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _enum_member(enum_type: type[StrEnum], value: object) -> StrEnum | None:
    if type(value) is not str:
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class InspirationCard:
    theme: Theme
    emotion: Emotion
    imagery: Imagery
    tone: Tone
    source: str = field(repr=False)
    rights_category: RightsCategory
    evidence: str = field(repr=False)
    discovery_timestamp: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.theme, Theme)
            or not isinstance(self.emotion, Emotion)
            or not isinstance(self.imagery, Imagery)
            or not isinstance(self.tone, Tone)
            or _canonical_source(self.source) is None
            or self.rights_category is not RightsCategory.UNKNOWN
            or not _is_bounded_single_line(
                self.evidence,
                MAX_RIGHTS_EVIDENCE_CHARACTERS,
            )
            or not isinstance(self.discovery_timestamp, datetime)
            or self.discovery_timestamp.tzinfo is not UTC
        ):
            raise _rejected()


def build_inspiration_card(
    draft: object,
    source_text: str,
    provenance: DiscoveryRecord,
) -> InspirationCard:
    """Bind an exact untrusted semantic draft to trusted transient provenance."""

    if (
        type(draft) is not dict
        or set(draft) != _DRAFT_KEYS
        or type(source_text) is not str
        or not (
            MIN_EXTRACTED_CHARACTERS
            <= len(source_text)
            <= MAX_EXTRACTED_CHARACTERS
        )
        or not isinstance(provenance, DiscoveryRecord)
        or not _is_bounded_single_line(
            provenance.result_id,
            MAX_RESULT_ID_CHARACTERS,
        )
        or RESULT_ID.fullmatch(provenance.result_id) is None
    ):
        raise _rejected()

    theme = _enum_member(Theme, draft["theme"])
    emotion = _enum_member(Emotion, draft["emotion"])
    imagery = _enum_member(Imagery, draft["imagery"])
    tone = _enum_member(Tone, draft["tone"])
    source = _canonical_source(provenance.source_url)
    discovered_at = _utc_timestamp(provenance.retrieved_at)
    if (
        not isinstance(theme, Theme)
        or not isinstance(emotion, Emotion)
        or not isinstance(imagery, Imagery)
        or not isinstance(tone, Tone)
        or source is None
        or discovered_at is None
        or not _is_bounded_single_line(
            provenance.rights_evidence,
            MAX_RIGHTS_EVIDENCE_CHARACTERS,
        )
    ):
        raise _rejected()

    semantic_signals = " ".join(
        (theme.value, emotion.value, imagery.value, tone.value)
    )
    if copies_source_span(semantic_signals, source_text):
        raise _rejected()

    return InspirationCard(
        theme=theme,
        emotion=emotion,
        imagery=imagery,
        tone=tone,
        source=source,
        rights_category=RightsCategory.UNKNOWN,
        evidence=provenance.rights_evidence,
        discovery_timestamp=discovered_at,
    )
