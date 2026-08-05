"""One-off measurement over the committed T10 qualification results.

Replays the 99 recorded, accepted outputs from
`evals/t10/qualification-results-2026-08-05.json` through this repo's own
duplicate metric (`fuzz.token_sort_ratio` on `normalize_text`,
`NEAR_DUPLICATE_THRESHOLD = 79.0`, both from
`personal_voice_msg.history`/`personal_voice_msg.normalization`) against an
*accumulating* shared history, in original corpus order (`testIdx`), rather
than the fresh-per-trial-database isolation the real qualification harness
used (each trial got its own empty temporary database by design -- see
`docs/task-logs/T10.md`).

Makes zero API calls. Reads only the already-saved, committed results JSON.
Kept in the repo for reproducibility of the figure recorded in
`docs/task-logs/T10.md`; not part of the qualification harness itself and
not imported by `provider.py` or `promptfooconfig.yaml`.

Usage:
    uv run python evals/t10/measure_accumulating_duplicates.py
"""

from __future__ import annotations

import json
from pathlib import Path

from rapidfuzz import fuzz

from personal_voice_msg.normalization import normalize_text, normalized_hash

NEAR_DUPLICATE_THRESHOLD = 79.0

RESULTS_PATH = Path(__file__).parent / "qualification-results-2026-08-05.json"


def load_ordered_outputs(results_path: Path) -> list[str]:
    with results_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    results = data["results"]["results"]
    results_sorted = sorted(results, key=lambda row: row["testIdx"])

    outputs: list[str] = []
    for row in results_sorted:
        output = row["response"]["output"]
        if output.startswith("T10_QUALIFICATION_FAILURE"):
            continue
        outputs.append(output)
    return outputs


def count_accumulating_duplicates(outputs: list[str]) -> tuple[int, int]:
    """Return (unique_count, rejected_count) replaying `outputs` in order."""
    accepted_history: list[str] = []
    accepted_hashes: set[str] = set()
    unique_count = 0
    rejected_count = 0

    for candidate in outputs:
        normalized_candidate = normalize_text(candidate)
        candidate_hash = normalized_hash(candidate)

        if candidate_hash in accepted_hashes:
            rejected_count += 1
            continue

        best_score = 0.0
        for prior in accepted_history:
            score = float(
                fuzz.token_sort_ratio(
                    normalized_candidate,
                    normalize_text(prior),
                    processor=None,
                )
            )
            best_score = max(best_score, score)

        if best_score >= NEAR_DUPLICATE_THRESHOLD:
            rejected_count += 1
            continue

        unique_count += 1
        accepted_history.append(candidate)
        accepted_hashes.add(candidate_hash)

    return unique_count, rejected_count


def main() -> None:
    outputs = load_ordered_outputs(RESULTS_PATH)
    unique_count, rejected_count = count_accumulating_duplicates(outputs)
    print(f"Recorded accepted outputs replayed: {len(outputs)}")
    print(f"Unique outputs accepted into accumulating history: {unique_count}")
    print(f"Rejected as near-duplicate of an earlier output: {rejected_count}")


if __name__ == "__main__":
    main()
