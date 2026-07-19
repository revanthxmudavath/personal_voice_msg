from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_voice_msg.database import Database
from personal_voice_msg.history import MessageHistory

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

CorpusPair = tuple[str, str, str]

KNOWN_DUPLICATES: tuple[CorpusPair, ...] = (
    (
        "case-and-punctuation",
        "Your smile makes every quiet morning feel brighter.",
        "YOUR SMILE—MAKES EVERY QUIET MORNING FEEL BRIGHTER!",
    ),
    (
        "unicode-width",
        "You bring a gentle glow to my ordinary afternoons.",
        "Ｙｏｕ bring a gentle glow to my ordinary afternoons!",
    ),
    (
        "word-order",
        "Your laughter turns ordinary afternoons into little celebrations.",
        "Ordinary afternoons turn into little celebrations with your laughter.",
    ),
    (
        "kindness-reordered",
        "I love how your kindness makes difficult days feel lighter.",
        "Difficult days feel lighter because of your kindness, and I love that.",
    ),
    (
        "smile-reordered",
        "Your smile makes even the quietest mornings feel full of light.",
        "Even the quietest mornings feel full of light when you smile.",
    ),
    (
        "gentle-typos",
        "Your kindness makes every morning brighter.",
        "Y0ur kindnes makez evry mornng brightr.",
    ),
    (
        "restless-thoughts",
        "Your calm voice makes every restless thought settle gently.",
        "Every restless thought settles gently when I hear your calm voice.",
    ),
    (
        "cherished-mornings",
        "I cherish the gentle way you brighten my slow mornings.",
        "The gentle way you brighten my slow mornings is something I cherish.",
    ),
    (
        "rainy-evenings",
        "Your warm laugh makes every rainy evening feel cozy.",
        "Every rainy evening feels cozy because of your warm laugh.",
    ),
    (
        "closeness-reordered",
        "I love how calm I feel whenever you are close.",
        "Whenever you are close, I love how calm I feel.",
    ),
    (
        "thoughtful-messages",
        "Your thoughtful messages always make my afternoons sweeter.",
        "My afternoons always feel sweeter because of your thoughtful messages.",
    ),
    (
        "small-typos",
        "Being with you makes simple moments feel wonderfully special.",
        "Being with you makes simpel moments feel wonderfullly special.",
    ),
    (
        "unicode-combining",
        "Our café mornings always leave me smiling.",
        "Our cafe\u0301 mornings always leave me smiling!",
    ),
    (
        "soft-evening",
        "Your gentle presence gives every evening a softer glow.",
        "Every evening has a softer glow because of your gentle presence.",
    ),
)

KNOWN_DISTINCT: tuple[CorpusPair, ...] = (
    (
        "laughter-versus-rest",
        "Your laughter makes ordinary afternoons feel bright.",
        "I hope tonight brings you deep rest and an easy morning.",
    ),
    (
        "rain-versus-coffee",
        "Being near you makes rainy evenings feel warm.",
        "I saved a quiet thought of you for my first coffee today.",
    ),
    (
        "smile-versus-courage",
        "Your smile brings a little sunlight into every room.",
        "I admire the steady courage you bring to difficult days.",
    ),
    (
        "voice-versus-kindness",
        "Your voice makes my restless thoughts settle gently.",
        "The kindness you show strangers makes me proud to know you.",
    ),
    (
        "morning-versus-evening",
        "I love the hopeful energy you carry into each morning.",
        "May this evening give your busy mind a peaceful place to land.",
    ),
    (
        "patience-versus-playfulness",
        "Your patience makes complicated moments feel manageable.",
        "Your playful ideas turn simple plans into tiny adventures.",
    ),
    (
        "presence-versus-message",
        "Your calm presence makes crowded places feel comfortable.",
        "A message from you can make an ordinary afternoon feel special.",
    ),
    (
        "eyes-versus-generosity",
        "The sparkle in your eyes makes me smile without trying.",
        "I notice how generously you make room for other people.",
    ),
    (
        "quiet-versus-energy",
        "I treasure the quiet ease I feel when we talk.",
        "Your bright energy makes even a slow Monday feel possible.",
    ),
    (
        "memory-versus-future",
        "Thinking about your warm laugh made my commute lighter.",
        "I hope tomorrow surprises you with something genuinely lovely.",
    ),
    (
        "high-overlap-changed-focus",
        "Your calm presence makes every busy morning feel peaceful.",
        "I hope your busy morning gives you one peaceful moment to yourself.",
    ),
    (
        "high-overlap-changed-action",
        "I love how your patience makes long afternoons feel easy.",
        "I hope this long afternoon gives you time to be patient with yourself.",
    ),
)

# Token-sort comparison deliberately ignores order. These distinct meanings are
# conservative rejections we accept at T04 rather than risk a missed duplicate.
ACCEPTABLE_FALSE_POSITIVES: tuple[CorpusPair, ...] = (
    (
        "same-words-different-speaker",
        "Your gentle words make my difficult mornings feel lighter.",
        "My gentle words make your difficult mornings feel lighter.",
    ),
    (
        "same-words-different-action",
        "Your quiet smile makes my busy thoughts slow down.",
        "My quiet thoughts make your busy smile slow down.",
    ),
)


def create_history(path: Path) -> MessageHistory:
    database = Database(path)
    database.migrate()
    return MessageHistory(database)


def seed_history(history: MessageHistory, text: str) -> None:
    decision = history.evaluate_and_record(text, now=NOW)

    assert decision.accepted, "the empty history must accept its seed message"


@pytest.mark.fast
@pytest.mark.parametrize(
    ("label", "existing", "candidate"),
    KNOWN_DUPLICATES,
    ids=[pair[0] for pair in KNOWN_DUPLICATES],
)
def test_curated_duplicates_have_zero_known_false_negatives(
    tmp_path: Path,
    label: str,
    existing: str,
    candidate: str,
) -> None:
    history = create_history(tmp_path / f"{label}.sqlite3")
    seed_history(history, existing)

    decision = history.evaluate_and_record(candidate, now=NOW)

    assert not decision.accepted, f"known duplicate escaped: {label}"


@pytest.mark.fast
@pytest.mark.parametrize(
    ("label", "existing", "candidate"),
    KNOWN_DISTINCT,
    ids=[pair[0] for pair in KNOWN_DISTINCT],
)
def test_curated_distinct_messages_remain_accepted(
    tmp_path: Path,
    label: str,
    existing: str,
    candidate: str,
) -> None:
    history = create_history(tmp_path / f"{label}.sqlite3")
    seed_history(history, existing)

    decision = history.evaluate_and_record(candidate, now=NOW)

    assert decision.accepted, f"unexpected false positive: {label}"


@pytest.mark.fast
@pytest.mark.parametrize(
    ("label", "existing", "candidate"),
    ACCEPTABLE_FALSE_POSITIVES,
    ids=[pair[0] for pair in ACCEPTABLE_FALSE_POSITIVES],
)
def test_conservative_false_positives_are_explicitly_documented(
    tmp_path: Path,
    label: str,
    existing: str,
    candidate: str,
) -> None:
    history = create_history(tmp_path / f"{label}.sqlite3")
    seed_history(history, existing)

    decision = history.evaluate_and_record(candidate, now=NOW)

    assert not decision.accepted, f"documented false positive changed: {label}"
