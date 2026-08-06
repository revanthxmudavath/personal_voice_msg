from __future__ import annotations

import re
from dataclasses import dataclass

from personal_voice_msg.normalization import normalize_text


@dataclass(frozen=True, slots=True)
class GateViolation:
    category: str
    matched_phrase: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    accepted: bool
    violations: tuple[GateViolation, ...]


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")


SEXUAL_CONTENT_PHRASES = (
    "sex",
    "sexy",
    "sexual",
    "naked",
    "nude",
    "seduce",
    "seduction",
    "aroused",
    "orgasm",
    "make love",
    "turn you on",
)
POSSESSIVENESS_PHRASES = (
    "you belong to me",
    "belongs to me",
    "mine alone",
    "only mine",
    "i own you",
    "no one else can have you",
    "you are mine",
    "you re mine",
)
MANIPULATION_GUILT_PHRASES = (
    "if you loved me",
    "after everything i have done for you",
    "after everything i ve done for you",
    "you never appreciate me",
    "you owe me",
    "guilt trip",
    "look what you made me do",
)
BREAKUP_PHRASES = (
    "breaking up with you",
    "we are breaking up",
    "we re breaking up",
    "it is over between us",
    "it s over between us",
    "its over between us",
    "i do not love you anymore",
    "i don t love you anymore",
    "i dont love you anymore",
    "we are done",
    "we re done",
    "this relationship is over",
)
PROPOSAL_COMMITMENT_PHRASES = (
    "marry me",
    "will you marry me",
    "let us get married",
    "lets get married",
    "let us move in together",
    "lets move in together",
    "i want to have kids with you",
    "i want to have children with you",
    "spend the rest of my life with you",
)
MONEY_REQUEST_PHRASES = (
    "send me money",
    "wire me money",
    "send money",
    "need cash",
    "pay my rent",
    "venmo me",
    "paypal me",
    "send me a gift card",
    "i need money",
)
INSULT_PHRASES = (
    "you are stupid",
    "you re stupid",
    "you are worthless",
    "you re worthless",
    "you are ugly",
    "you re ugly",
    "idiot",
    "pathetic",
    "you are a loser",
    "you re a loser",
)
FABRICATED_MEMORY_PHRASES = (
    "remember when we",
    "remember that time we",
    "remember our trip to",
    "that day we",
    "our trip to",
    "the night we met",
    "the day we met",
    "last summer when we",
    "back when we",
    "on our anniversary we",
)
EXCESSIVE_INTENSITY_PHRASES = (
    "more than anything in the universe",
    "forever and ever and ever",
    "i would die without you",
    "i cannot live without you",
    "i can not live without you",
    "i can t live without you",
    "you are my entire universe",
    "the only reason i breathe",
)
PROMPT_INJECTION_PHRASES = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard the above",
    "disregard previous instructions",
    "you are now",
    "act as if you are",
    "reveal your system prompt",
    "reveal your instructions",
    "print your prompt",
    "new instructions",
    "system prompt",
)

# Curated, deliberately non-exhaustive: this is the deterministic gate's
# high-confidence pass. The structured judge (judging/judge.py) is the
# semantic backstop for a stranger name, or any other category, that a
# fixed list like this one does not happen to contain.
STRANGER_NAME_TOKENS = frozenset(
    {
        "james", "john", "robert", "michael", "david", "william", "richard",
        "joseph", "thomas", "daniel", "mark", "paul", "steven", "andrew",
        "kevin", "brian", "mary", "patricia", "jennifer", "linda",
        "elizabeth", "susan", "jessica", "sarah", "karen", "nancy", "lisa",
        "emily", "amanda", "melissa", "ashley", "rachel", "michelle",
        "laura", "kimberly", "amy", "angela", "stephanie", "priya",
        "carlos", "wei", "fatima",
    }
)

_PHRASE_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sexual_content", SEXUAL_CONTENT_PHRASES),
    ("possessiveness", POSSESSIVENESS_PHRASES),
    ("manipulation_guilt", MANIPULATION_GUILT_PHRASES),
    ("breakup_language", BREAKUP_PHRASES),
    ("proposal_or_commitment", PROPOSAL_COMMITMENT_PHRASES),
    ("money_request", MONEY_REQUEST_PHRASES),
    ("insult", INSULT_PHRASES),
    ("fabricated_memory", FABRICATED_MEMORY_PHRASES),
    ("excessive_emotional_intensity", EXCESSIVE_INTENSITY_PHRASES),
    ("prompt_injection", PROMPT_INJECTION_PHRASES),
)

# Compiled once at import time -- the phrase lists above are static module
# constants, so there is no reason to recompile the same ~95 regexes on
# every evaluate_gates() call.
_COMPILED_PHRASE_CATEGORIES: tuple[
    tuple[str, tuple[tuple[str, re.Pattern[str]], ...]], ...
] = tuple(
    (category, tuple((phrase, _phrase_pattern(phrase)) for phrase in phrases))
    for category, phrases in _PHRASE_CATEGORIES
)


def evaluate_gates(candidate: str) -> GateDecision:
    normalized = normalize_text(candidate)
    violations: list[GateViolation] = []

    for category, phrase_patterns in _COMPILED_PHRASE_CATEGORIES:
        for phrase, pattern in phrase_patterns:
            if pattern.search(normalized):
                violations.append(GateViolation(category, phrase))
                break

    for token in normalized.split():
        if token in STRANGER_NAME_TOKENS:
            violations.append(GateViolation("stranger_name", token))
            break

    if candidate.count("!") > 1:
        violations.append(GateViolation("excessive_emotional_intensity", "!!"))

    return GateDecision(accepted=not violations, violations=tuple(violations))
