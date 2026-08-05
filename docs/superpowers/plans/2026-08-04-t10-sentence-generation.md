# T10 Original English Sentence Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate exactly one original, structurally-valid, non-plagiarized English sentence from a sanitized `InspirationCard`, using a real (never mocked) call to the Gemini API, with deterministic fail-closed validation.

**Architecture:** A new `generation/` subpackage with three layers matching this repo's existing separation of pure logic from real-boundary I/O (mirrors `discovery/web.py` vs `discovery/baseline.py`, and `history.py`'s `_evaluate_with_connection` split): (1) a narrow secret-loading module reusing T02's existing boundary-validation primitives, kept independent of the sender/voice `Settings` to preserve the discovery/generation-vs-sender isolation `AGENTS.md`'s network rules already mandate; (2) a narrow `aiohttp` HTTP client boundary to Gemini's `generateContent` endpoint, with response-parsing split into a pure function; (3) prompt construction and sentence validation, orchestrating the client + reusing T04's existing `MessageHistory`/`copies_source_span`. A fourth task adds the Promptfoo-orchestrated 100-trial qualification run that is T10's actual done-when gate.

**Tech Stack:** Python 3.12, `aiohttp` (already a dependency), real Gemini API (`gemini-3.6-flash`), Promptfoo (Node, installed in Task 4 only).

## Global Constraints

- Model pin (owner-confirmed for T10, independent of T09's benchmark-only pin): `gemini-3.6-flash`.
- Client: hand-rolled `aiohttp` HTTP boundary, not the `google-genai` SDK (removed after T09; not reintroduced).
- Endpoint (verified with a real call, 2026-08-04): `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`, auth via `x-goog-api-key` header — never in the URL, never logged.
- `generationConfig.temperature = 0.2` (plan text: "low temperature").
- `generationConfig.maxOutputTokens = 2048`. Verified empirically: this model spends a large, non-deterministic number of invisible "thinking" tokens before visible output (189 and 511 tokens observed across two identical real calls); a tight budget (200) silently truncates before any content appears (`finishReason: "MAX_TOKENS"`). `thinkingConfig.thinkingBudget: 0` is rejected outright (HTTP 400) for this model — there is no way to disable thinking, only to budget generously past it.
- `generationConfig.responseMimeType = "application/json"`, `responseSchema = {"type": "OBJECT", "properties": {"sentence": {"type": "STRING"}}, "required": ["sentence"]}` (uppercase type names — verified real, working shape).
- The real response's actual sentence is a JSON **string** inside `candidates[0].content.parts[0].text` — it requires a second `json.loads()`, it is not a native nested object.
- Any `candidates[0].finishReason` other than `"STOP"` is a hard reject (covers `MAX_TOKENS` truncation and is also the natural fail-closed hook for `RECITATION`, which Gemini can set when output closely matches training data — directly relevant to this project's anti-plagiarism requirement).
- Generation receives only `InspirationCard.theme`, `.emotion`, `.imagery`, `.tone` — never `.source`, `.evidence` (both already marked `field(repr=False)` in `inspiration.py`, i.e. deliberately kept out of casual exposure), never `.discovery_timestamp` or `.rights_category`. This matches the plan's existing rule: "The generator receives a sanitized InspirationCard, not the full source passage."
- **Scoping decision** (stated per Karpathy guidance — flag, don't silently pick, when an interpretation choice exists): T10's own deterministic code enforces exactly what T10's dependency chain (T04 + T08, not T11) can enforce without a semantic judge — single-sentence structure, no URL, no copied/near-copied source wording (reusing T04's `copies_source_span` and `MessageHistory`). "No stranger names / fabricated memories" is T11's explicit job (a separate later task, red corpus includes "stranger names," "fabricated memories," depends on T04 **and T10**, not the reverse) via a structured LLM judge — that is a second, independent model call this plan does not build. T10's own 100-trial qualification (Task 4) verifies empirically, by inspecting the real trial outputs, that the model does not drift into those failure modes; it does not add a bespoke classifier for them.
- **Observed pre-existing gap, explicitly out of scope for this plan**: nothing in the codebase currently writes to the `sources` or `inspiration_cards` SQL tables, and `messages.inspiration_card_id` is set by nothing (grep-verified). T10's red tests and implementation bullets in `IMPLEMENTATION_PLAN.md` do not mention this, and the column is nullable, so this plan leaves `inspiration_card_id` NULL, consistent with the plan text as written. Flagging this for the record, not fixing it here — a future task will need to close it before full provenance chains are required.
- No-mock policy: all client/orchestration tests that touch the network use the real Gemini API and are marked `@pytest.mark.live`, gated by `T10_LIVE_GENERATION=1` (module-level skip otherwise) — matching this repo's existing `T07_LIVE_DISCOVERY=1` convention exactly (`tests/live/test_discovery_baseline_live.py:28`). The real key file path for live tests comes from the `GEMINI_API_KEY_FILE` environment variable (matching the existing `SEARXNG_URL`-style value-injection pattern in `tests/integration/test_discovery_baseline_network.py:45`) — never hardcoded into committed source.
- Real key file already provisioned by the owner at `C:\Users\DELL\.personal_voice_msg\secrets\development\gemini_api_key.txt` (53 bytes, confirmed to exist and to authenticate successfully against the real API during plan verification). Never read its contents in any task except through the loader being built; never print it.

---

## File Structure

```
src/personal_voice_msg/
├── config.py                          # MODIFY: export 3 existing private helpers for reuse
└── generation/                        # NEW package
    ├── __init__.py
    ├── config.py                      # GeminiSettings + load_gemini_settings()
    ├── gemini_client.py                # generate_structured() + pure response parser
    └── sentence.py                     # build_prompt() + validate_generated_sentence() + generate_sentence()

tests/fast/
├── test_generation_config.py           # NEW
├── test_generation_gemini_client_parsing.py   # NEW (pure parser, fast)
└── test_generation_sentence.py         # NEW (pure validation, fast)

tests/live/
├── test_generation_gemini_client_live.py   # NEW (real API call)
└── test_generation_sentence_live.py    # NEW (real end-to-end generate_sentence)
```

`generation/` is created fresh — it did not exist before this plan (confirmed empty `src/personal_voice_msg/` listing before Task 1). This matches the repo layout target in `IMPLEMENTATION_PLAN.md` §8, which already lists `generation/` as a subpackage created "only when their first task needs them."

---

### Task 1: Export reusable config-boundary helpers, add generation secret loading

**Files:**
- Modify: `src/personal_voice_msg/config.py`
- Create: `src/personal_voice_msg/generation/__init__.py`
- Create: `src/personal_voice_msg/generation/config.py`
- Test: `tests/fast/test_generation_config.py`

**Interfaces:**
- Consumes: `personal_voice_msg.config.ConfigurationError`, `RuntimeProfile`, `SensitiveValue` (from `personal_voice_msg.redaction`).
- Produces: `personal_voice_msg.config.read_toml(config_path: Path, required_settings: set[str]) -> dict[str, Any]`, `runtime_profile(value: str) -> RuntimeProfile`, `secret_root(config_path: Path, value: str, profile: RuntimeProfile) -> Path`, `secret_file(root: Path, value: str, setting: str) -> Path` (all exported, previously private). `personal_voice_msg.generation.config.GeminiSettings` (frozen dataclass: `profile: RuntimeProfile`, `api_key: SensitiveValue[str]`) and `load_gemini_settings(config_path: Path) -> GeminiSettings` — Task 3 imports both.

- [ ] **Step 1: Export the three reusable helpers in `config.py` (rename only, no behavior change)**

In `src/personal_voice_msg/config.py`, rename (drop leading underscore, no other changes):
- `_read_toml` → `read_toml`, and change its signature from `_read_toml(config_path: Path) -> dict[str, Any]` to `read_toml(config_path: Path, required_settings: set[str]) -> dict[str, Any]`. Inside the function body, replace every use of the module-level `REQUIRED_SETTINGS` constant with the new `required_settings` parameter.
- `_runtime_profile` → `runtime_profile`
- `_secret_root` → `secret_root`
- `_secret_file` → `secret_file`

Update the 5 internal call sites inside `load_settings()` (currently `_read_toml(path)`, `_runtime_profile(...)`, `_secret_root(...)`, and 4 calls to `_secret_file(...)`) to use the new names, and pass `REQUIRED_SETTINGS` explicitly at the one `read_toml` call site: `read_toml(path, REQUIRED_SETTINGS)`.

- [ ] **Step 2: Run the existing config test suite to confirm the rename is behavior-neutral**

Run: `uv run pytest tests/fast/test_configuration.py -v`
Expected: all 32 tests still PASS (they only exercise `load_settings()`, never reference the renamed private helpers directly).

- [ ] **Step 3: Write the failing tests for generation config loading**

Create `tests/fast/test_generation_config.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from personal_voice_msg.generation.config import (
    GENERATION_REQUIRED_SETTINGS,
    load_gemini_settings,
)
from personal_voice_msg.config import ConfigurationError, RuntimeProfile


def write_generation_toml(path: Path, values: dict[str, str]) -> None:
    import json

    path.write_text(
        "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def create_generation_configuration(root: Path, *, key_content: str = "test-gemini-key") -> Path:
    secret_root_dir = root / "secrets"
    secret_root_dir.mkdir()
    (secret_root_dir / "gemini_api_key.txt").write_text(key_content, encoding="utf-8")

    config_path = root / "generation-settings.toml"
    write_generation_toml(
        config_path,
        {
            "profile": "development",
            "secret_root": secret_root_dir.as_posix(),
            "gemini_api_key_file": "gemini_api_key.txt",
        },
    )
    return config_path


@pytest.mark.fast
def test_loads_gemini_api_key_as_sensitive_value(tmp_path: Path) -> None:
    config_path = create_generation_configuration(tmp_path, key_content="real-looking-key-value")

    settings = load_gemini_settings(config_path)

    assert settings.profile is RuntimeProfile.DEVELOPMENT
    assert not isinstance(settings.api_key, str)
    assert settings.api_key.reveal() == "real-looking-key-value"


@pytest.mark.fast
def test_missing_gemini_api_key_file_fails_closed(tmp_path: Path) -> None:
    config_path = create_generation_configuration(tmp_path)
    (Path(tmp_path) / "secrets" / "gemini_api_key.txt").unlink()

    with pytest.raises(ConfigurationError):
        load_gemini_settings(config_path)


@pytest.mark.fast
def test_oversized_gemini_api_key_file_fails_closed(tmp_path: Path) -> None:
    config_path = create_generation_configuration(tmp_path)
    key_path = tmp_path / "secrets" / "gemini_api_key.txt"
    key_path.write_text("a" * 10_000_000, encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_gemini_settings(config_path)


@pytest.mark.fast
def test_unknown_generation_setting_fails_closed(tmp_path: Path) -> None:
    config_path = create_generation_configuration(tmp_path)
    with open(config_path, "a", encoding="utf-8") as f:
        f.write('extra_setting = "not-allowed"\n')

    with pytest.raises(ConfigurationError):
        load_gemini_settings(config_path)


@pytest.mark.fast
def test_generation_required_settings_are_exactly_three() -> None:
    assert GENERATION_REQUIRED_SETTINGS == {
        "profile",
        "secret_root",
        "gemini_api_key_file",
    }
```

- [ ] **Step 4: Run the new tests to verify they fail for the intended reason**

Run: `uv run pytest tests/fast/test_generation_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'personal_voice_msg.generation'`

- [ ] **Step 5: Create the `generation` package and the config loader**

Create `src/personal_voice_msg/generation/__init__.py`:

```python
"""Original English sentence generation from sanitized InspirationCards."""
```

Create `src/personal_voice_msg/generation/config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from personal_voice_msg.config import (
    ConfigurationError,
    RuntimeProfile,
    read_toml,
    runtime_profile,
    secret_file,
    secret_root,
)
from personal_voice_msg.redaction import SensitiveValue

GENERATION_REQUIRED_SETTINGS = {"profile", "secret_root", "gemini_api_key_file"}
MAX_GEMINI_API_KEY_CHARACTERS = 4_096


@dataclass(frozen=True, slots=True)
class GeminiSettings:
    profile: RuntimeProfile
    api_key: SensitiveValue[str]


def _api_key(path: Path) -> str:
    try:
        oversized = path.stat().st_size > MAX_GEMINI_API_KEY_CHARACTERS
    except OSError:
        raise ConfigurationError("Gemini API key file is unreadable") from None
    if oversized:
        raise ConfigurationError("Gemini API key file is too large")
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ConfigurationError("Gemini API key file is unreadable") from None
    if not key:
        raise ConfigurationError("Gemini API key is empty")
    return key


def load_gemini_settings(config_path: Path) -> GeminiSettings:
    """Load non-secret TOML settings and the Gemini API key from a bounded file."""

    path = config_path.resolve()
    document = read_toml(path, GENERATION_REQUIRED_SETTINGS)
    profile = runtime_profile(document["profile"])
    root = secret_root(path, document["secret_root"], profile)
    key_path = secret_file(
        root, document["gemini_api_key_file"], "gemini_api_key_file"
    )
    return GeminiSettings(profile=profile, api_key=SensitiveValue(_api_key(key_path)))
```

- [ ] **Step 6: Run the tests until green**

Run: `uv run pytest tests/fast/test_generation_config.py -v`
Expected: 5 passed

- [ ] **Step 7: Run the full fast suite and lint/type checks**

Run: `uv run pytest -m fast -q && uv run ruff check . && uv run mypy src`
Expected: all pass, no regressions in the 356 pre-existing tests.

- [ ] **Step 8: Commit**

```bash
git add src/personal_voice_msg/config.py src/personal_voice_msg/generation/ tests/fast/test_generation_config.py
git commit -m "T10: add generation secret-boundary config loader"
```

---

### Task 2: Gemini HTTP client boundary

**Files:**
- Create: `src/personal_voice_msg/generation/gemini_client.py`
- Test: `tests/fast/test_generation_gemini_client_parsing.py`
- Test: `tests/live/test_generation_gemini_client_live.py`

**Interfaces:**
- Consumes: `personal_voice_msg.redaction.SensitiveValue` (Task 1's `GeminiSettings.api_key` is passed in by the caller in Task 3).
- Produces: `GeminiGenerationConfig` (frozen dataclass: `model: str`, `temperature: float`, `max_output_tokens: int`, `response_schema: dict[str, object]`), `GeminiClientError(RuntimeError)`, `async def generate_structured(session: aiohttp.ClientSession, api_key: SensitiveValue[str], prompt: str, config: GeminiGenerationConfig) -> dict[str, object]` — Task 3's `generate_sentence()` calls this directly.

- [ ] **Step 1: Write the failing fast tests for response parsing (pure function, no network)**

Create `tests/fast/test_generation_gemini_client_parsing.py`. These fixtures are the **real, empirically-verified response shapes** observed during plan verification (2026-08-04), not fabricated:

```python
from __future__ import annotations

import pytest

from personal_voice_msg.generation.gemini_client import (
    GeminiClientError,
    _parse_generate_content_response,
)

REAL_STOP_RESPONSE = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {"text": '{"sentence":"Your gentle heart has a wonderful way of making every day feel brighter."}'}
                ],
                "role": "model",
            },
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 28,
        "candidatesTokenCount": 18,
        "totalTokenCount": 557,
        "thoughtsTokenCount": 511,
    },
    "modelVersion": "gemini-3.6-flash",
    "responseId": "cG9yau-CJ_SEz7IPh7rOEQ",
}

REAL_MAX_TOKENS_RESPONSE = {
    "candidates": [
        {
            "content": {"parts": [{"text": "Here is the JSON"}], "role": "model"},
            "finishReason": "MAX_TOKENS",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 28,
        "candidatesTokenCount": 7,
        "totalTokenCount": 224,
        "thoughtsTokenCount": 189,
    },
    "modelVersion": "gemini-3.6-flash",
    "responseId": "Pm9yarihGNKyqtsPj528sQ4",
}


@pytest.mark.fast
def test_parses_real_stop_response_into_structured_dict() -> None:
    result = _parse_generate_content_response(REAL_STOP_RESPONSE)

    assert result == {
        "sentence": "Your gentle heart has a wonderful way of making every day feel brighter."
    }


@pytest.mark.fast
def test_rejects_max_tokens_finish_reason() -> None:
    with pytest.raises(GeminiClientError):
        _parse_generate_content_response(REAL_MAX_TOKENS_RESPONSE)


@pytest.mark.fast
@pytest.mark.parametrize(
    "broken_payload",
    [
        {},
        {"candidates": []},
        {"candidates": [{"finishReason": "STOP"}]},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]},
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "not valid json"}]},
                }
            ]
        },
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "[1, 2, 3]"}]},
                }
            ]
        },
    ],
    ids=["empty", "no-candidates", "no-content", "no-parts", "bad-json", "not-a-dict"],
)
def test_malformed_response_shapes_fail_closed(broken_payload: dict) -> None:
    with pytest.raises(GeminiClientError):
        _parse_generate_content_response(broken_payload)
```

- [ ] **Step 2: Run to verify failure for the intended reason**

Run: `uv run pytest tests/fast/test_generation_gemini_client_parsing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'personal_voice_msg.generation.gemini_client'`

- [ ] **Step 3: Implement the client module**

Create `src/personal_voice_msg/generation/gemini_client.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass

import aiohttp

from personal_voice_msg.redaction import SensitiveValue

GEMINI_API_HOST = "generativelanguage.googleapis.com"
GEMINI_API_VERSION = "v1beta"
MAX_RESPONSE_BYTES = 65_536
REQUEST_TIMEOUT_SECONDS = 30.0


class GeminiClientError(RuntimeError):
    """Report a bounded, non-leaking Gemini API call failure."""


@dataclass(frozen=True, slots=True)
class GeminiGenerationConfig:
    model: str
    temperature: float
    max_output_tokens: int
    response_schema: dict[str, object]


def _parse_generate_content_response(
    payload: dict[str, object],
) -> dict[str, object]:
    try:
        candidates = payload["candidates"]
        first = candidates[0]  # type: ignore[index]
        if first["finishReason"] != "STOP":  # type: ignore[index]
            raise GeminiClientError("Gemini generation did not finish cleanly")
        text = first["content"]["parts"][0]["text"]  # type: ignore[index]
        structured = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise GeminiClientError("Gemini response was malformed") from None
    if not isinstance(structured, dict):
        raise GeminiClientError("Gemini response was malformed")
    return structured


async def generate_structured(
    session: aiohttp.ClientSession,
    api_key: SensitiveValue[str],
    prompt: str,
    config: GeminiGenerationConfig,
) -> dict[str, object]:
    """Call the real Gemini generateContent boundary and return the parsed reply."""

    url = (
        f"https://{GEMINI_API_HOST}/{GEMINI_API_VERSION}/models/"
        f"{config.model}:generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": config.temperature,
            "maxOutputTokens": config.max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": config.response_schema,
        },
    }
    try:
        async with session.post(
            url,
            json=body,
            headers={"x-goog-api-key": api_key.reveal()},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GeminiClientError("Gemini response exceeded the size limit")
            if response.status != 200:
                raise GeminiClientError("Gemini API call failed")
            payload = json.loads(raw)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
        raise GeminiClientError("Gemini API call failed") from None

    return _parse_generate_content_response(payload)
```

- [ ] **Step 4: Run the fast tests until green**

Run: `uv run pytest tests/fast/test_generation_gemini_client_parsing.py -v`
Expected: 8 passed

- [ ] **Step 5: Write the failing live test (real API call)**

Create `tests/live/test_generation_gemini_client_live.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.generation.gemini_client import (
    GeminiGenerationConfig,
    generate_structured,
)
from personal_voice_msg.redaction import SensitiveValue

if os.environ.get("T10_LIVE_GENERATION") != "1":
    pytestmark = [
        pytest.mark.skip(reason="requires T10_LIVE_GENERATION=1"),
    ]

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"sentence": {"type": "STRING"}},
    "required": ["sentence"],
}


def _real_api_key() -> SensitiveValue[str]:
    key_file = os.environ["GEMINI_API_KEY_FILE"]
    return SensitiveValue(Path(key_file).read_text(encoding="utf-8").strip())


async def _call_returns_a_sentence() -> dict[str, object]:
    config = GeminiGenerationConfig(
        model="gemini-3.6-flash",
        temperature=0.2,
        max_output_tokens=2048,
        response_schema=RESPONSE_SCHEMA,
    )
    async with aiohttp.ClientSession() as session:
        return await generate_structured(
            session,
            _real_api_key(),
            "Write one short, sweet, original sentence about appreciating "
            "someone's kindness. Do not use any quotes, lyrics, URLs, or names.",
            config,
        )


@pytest.mark.live
def test_real_gemini_call_returns_a_sentence() -> None:
    result = asyncio.run(_call_returns_a_sentence())

    assert isinstance(result["sentence"], str)
    assert len(result["sentence"]) > 0


async def _call_with_too_small_a_token_budget() -> None:
    config = GeminiGenerationConfig(
        model="gemini-3.6-flash",
        temperature=0.2,
        max_output_tokens=50,
        response_schema=RESPONSE_SCHEMA,
    )
    async with aiohttp.ClientSession() as session:
        await generate_structured(
            session,
            _real_api_key(),
            "Write one short, sweet, original sentence about appreciating "
            "someone's kindness.",
            config,
        )


@pytest.mark.live
def test_real_gemini_call_rejects_too_small_a_token_budget() -> None:
    with pytest.raises(Exception):
        asyncio.run(_call_with_too_small_a_token_budget())
```

No new test dependency is needed: each async call is wrapped in a plain
`async def` helper invoked via `asyncio.run()` from an ordinary sync test
function — `pytest-asyncio` is not required. Add `import asyncio` to this
file's imports.

- [ ] **Step 6: Run the live test for real, with the real key**

Run (PowerShell):
```powershell
$env:T10_LIVE_GENERATION = "1"
$env:GEMINI_API_KEY_FILE = "C:\Users\DELL\.personal_voice_msg\secrets\development\gemini_api_key.txt"
uv run pytest tests/live/test_generation_gemini_client_live.py -v -m live
```
Expected: 2 passed (real network calls to the real API).

- [ ] **Step 7: Run the full fast suite, lint, and type checks**

Run: `uv run pytest -m fast -q && uv run ruff check . && uv run mypy src`
Expected: all pass, no regressions.

- [ ] **Step 8: Commit**

```bash
git add src/personal_voice_msg/generation/gemini_client.py tests/fast/test_generation_gemini_client_parsing.py tests/live/test_generation_gemini_client_live.py pyproject.toml uv.lock
git commit -m "T10: add real Gemini generateContent HTTP client boundary"
```

---

### Task 3: Prompt construction and sentence validation

**Files:**
- Create: `src/personal_voice_msg/generation/sentence.py`
- Test: `tests/fast/test_generation_sentence.py`
- Test: `tests/live/test_generation_sentence_live.py`

**Interfaces:**
- Consumes: `personal_voice_msg.discovery.inspiration.InspirationCard` (and its `Theme`/`Emotion`/`Imagery`/`Tone` enums), `personal_voice_msg.history.MessageHistory` and `DuplicateReason` (Task from T04), `personal_voice_msg.normalization.copies_source_span`, Task 2's `generate_structured`/`GeminiGenerationConfig`/`GeminiClientError`, Task 1's `GeminiSettings`.
- Produces: `build_prompt(card: InspirationCard) -> str`, `class SentenceValidationError(RuntimeError)`, `validate_generated_sentence(raw: str, *, source_text: str | None = None) -> str` (pure — no DB), `async def generate_sentence(session: aiohttp.ClientSession, api_key: SensitiveValue[str], card: InspirationCard, history: MessageHistory, now: datetime, *, source_text: str | None = None) -> DuplicateDecision` — Task 4's Promptfoo provider calls this.

- [ ] **Step 1: Write the failing fast tests for prompt building and pure validation**

Create `tests/fast/test_generation_sentence.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from personal_voice_msg.discovery.inspiration import (
    Emotion,
    Imagery,
    InspirationCard,
    RightsCategory,
    Theme,
    Tone,
)
from personal_voice_msg.generation.sentence import (
    SentenceValidationError,
    build_prompt,
    validate_generated_sentence,
)

CARD = InspirationCard(
    theme=Theme.APPRECIATION,
    emotion=Emotion.WARMTH,
    imagery=Imagery.MORNING_LIGHT,
    tone=Tone.GENTLE,
    source="https://example.invalid/poem",
    rights_category=RightsCategory.UNKNOWN,
    evidence="unused in this test",
    discovery_timestamp=datetime(2026, 8, 4, tzinfo=UTC),
)


@pytest.mark.fast
def test_prompt_includes_all_four_semantic_signals_and_excludes_source() -> None:
    prompt = build_prompt(CARD)

    assert "appreciation" in prompt
    assert "warmth" in prompt
    assert "morning light" in prompt
    assert "gentle" in prompt
    assert "example.invalid" not in prompt
    assert "unused in this test" not in prompt


@pytest.mark.fast
def test_accepts_a_single_clean_sentence() -> None:
    result = validate_generated_sentence(
        "Your gentle heart has a wonderful way of making every day feel brighter."
    )
    assert result == (
        "Your gentle heart has a wonderful way of making every day feel brighter."
    )


@pytest.mark.fast
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Two sentences. Right here.",
        "No terminal punctuation at all",
        "Visit https://example.com for more.",
        "Check out www.example.com today.",
    ],
    ids=[
        "empty",
        "whitespace-only",
        "multi-sentence",
        "no-terminal-punctuation",
        "https-url",
        "www-url",
    ],
)
def test_rejects_structurally_invalid_output(raw: str) -> None:
    with pytest.raises(SentenceValidationError):
        validate_generated_sentence(raw)


@pytest.mark.fast
def test_rejects_six_consecutive_source_words() -> None:
    source = "The moon keeps a silver promise above the quiet sleeping city"
    candidate = "Tonight, a silver promise above the quiet sleeping city makes me smile."

    with pytest.raises(SentenceValidationError):
        validate_generated_sentence(candidate, source_text=source)
```

- [ ] **Step 2: Run to verify failure for the intended reason**

Run: `uv run pytest tests/fast/test_generation_sentence.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'personal_voice_msg.generation.sentence'`

- [ ] **Step 3: Implement `sentence.py`**

Create `src/personal_voice_msg/generation/sentence.py`:

```python
from __future__ import annotations

import re
from datetime import datetime

import aiohttp

from personal_voice_msg.discovery.inspiration import InspirationCard
from personal_voice_msg.generation.gemini_client import (
    GeminiClientError,
    GeminiGenerationConfig,
    generate_structured,
)
from personal_voice_msg.history import DuplicateDecision, MessageHistory
from personal_voice_msg.normalization import copies_source_span
from personal_voice_msg.redaction import SensitiveValue

MODEL = "gemini-3.6-flash"
TEMPERATURE = 0.2
MAX_OUTPUT_TOKENS = 2048
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"sentence": {"type": "STRING"}},
    "required": ["sentence"],
}
_URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
_TERMINAL_PUNCTUATION = (".", "!", "?")


class SentenceValidationError(RuntimeError):
    """Report a rejected generated sentence without including its text."""


def build_prompt(card: InspirationCard) -> str:
    imagery_phrase = card.imagery.value.replace("_", " ")
    return (
        "Write exactly one short, natural, spoken-style English sentence "
        f"expressing {card.theme.value} with a feeling of {card.emotion.value}, "
        f"evoking {imagery_phrase}, in a {card.tone.value} tone. "
        "Do not use any quotes, song lyrics, citations, URLs, names of real "
        "people, or references to a specific shared memory. Do not mention "
        "sex, money, marriage proposals, or breaking up. Return only the "
        "sentence."
    )


def validate_generated_sentence(
    raw: str, *, source_text: str | None = None
) -> str:
    candidate = raw.strip()
    if not candidate:
        raise SentenceValidationError("generated sentence rejected")
    if _URL_PATTERN.search(candidate):
        raise SentenceValidationError("generated sentence rejected")
    if candidate[-1] not in _TERMINAL_PUNCTUATION:
        raise SentenceValidationError("generated sentence rejected")
    if any(character in _TERMINAL_PUNCTUATION for character in candidate[:-1]):
        raise SentenceValidationError("generated sentence rejected")
    if source_text is not None and copies_source_span(candidate, source_text):
        raise SentenceValidationError("generated sentence rejected")
    return candidate


async def generate_sentence(
    session: aiohttp.ClientSession,
    api_key: SensitiveValue[str],
    card: InspirationCard,
    history: MessageHistory,
    now: datetime,
    *,
    source_text: str | None = None,
) -> DuplicateDecision:
    config = GeminiGenerationConfig(
        model=MODEL,
        temperature=TEMPERATURE,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        response_schema=RESPONSE_SCHEMA,
    )
    try:
        structured = await generate_structured(
            session, api_key, build_prompt(card), config
        )
        sentence = structured["sentence"]
    except (GeminiClientError, KeyError, TypeError):
        raise SentenceValidationError("generated sentence rejected") from None
    if not isinstance(sentence, str):
        raise SentenceValidationError("generated sentence rejected")

    validated = validate_generated_sentence(sentence, source_text=source_text)
    return history.evaluate_and_record(validated, now, source_text=source_text)
```

- [ ] **Step 4: Run the fast tests until green**

Run: `uv run pytest tests/fast/test_generation_sentence.py -v`
Expected: 9 passed

- [ ] **Step 5: Write the failing live end-to-end test**

Create `tests/live/test_generation_sentence_live.py`:

```python
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.database import Database
from personal_voice_msg.discovery.inspiration import (
    Emotion,
    Imagery,
    InspirationCard,
    RightsCategory,
    Theme,
    Tone,
)
from personal_voice_msg.generation.sentence import generate_sentence
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.redaction import SensitiveValue

if os.environ.get("T10_LIVE_GENERATION") != "1":
    pytestmark = [
        pytest.mark.skip(reason="requires T10_LIVE_GENERATION=1"),
    ]

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _real_api_key() -> SensitiveValue[str]:
    key_file = os.environ["GEMINI_API_KEY_FILE"]
    return SensitiveValue(Path(key_file).read_text(encoding="utf-8").strip())


async def _generate(card: InspirationCard, history: MessageHistory) -> object:
    async with aiohttp.ClientSession() as session:
        return await generate_sentence(session, _real_api_key(), card, history, NOW)


@pytest.mark.live
def test_real_generation_produces_an_accepted_unique_sentence(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "history.sqlite3")
    database.migrate()
    history = MessageHistory(database)
    card = InspirationCard(
        theme=Theme.ENCOURAGEMENT,
        emotion=Emotion.JOY,
        imagery=Imagery.OPEN_SKY,
        tone=Tone.PLAYFUL,
        source="https://example.invalid/poem",
        rights_category=RightsCategory.UNKNOWN,
        evidence="unused",
        discovery_timestamp=NOW,
    )

    decision = asyncio.run(_generate(card, history))

    assert decision.accepted
    assert decision.recorded_message_id is not None
```

Add `import asyncio` to this file's imports. No new test dependency needed,
matching Task 2's live test.

- [ ] **Step 6: Run the live test for real**

Run (PowerShell, same env vars as Task 2 Step 6):
```powershell
uv run pytest tests/live/test_generation_sentence_live.py -v -m live
```
Expected: 1 passed.

- [ ] **Step 7: Run the full fast suite, lint, and type checks**

Run: `uv run pytest -m fast -q && uv run ruff check . && uv run mypy src`
Expected: all pass, no regressions.

- [ ] **Step 8: Commit**

```bash
git add src/personal_voice_msg/generation/sentence.py tests/fast/test_generation_sentence.py tests/live/test_generation_sentence_live.py
git commit -m "T10: add prompt construction and sentence validation"
```

---

### Task 4: Promptfoo 100-trial qualification harness

**Files:**
- Create: `evals/t10/promptfooconfig.yaml`
- Create: `evals/t10/provider.py`
- Create: `evals/t10/corpus.yaml`
- Create: `docs/task-logs/T10.md`

**Interfaces:**
- Consumes: Task 3's `generate_sentence`, Task 1's `load_gemini_settings`.
- Produces: the T10 done-when evidence recorded in `docs/task-logs/T10.md`.

This task is evidence-gathering, not new red/green pytest cycles — Tasks 1-3 are the deterministic release gate per `IMPLEMENTATION_PLAN.md`'s "Evaluation and guardrail policy" ("Deterministic pytest and security tests remain the authoritative release gates. An evaluation framework may orchestrate trials and report metrics but cannot authorize a candidate"). Promptfoo here only orchestrates volume and records results.

- [ ] **Step 1: Install Promptfoo** (Node `v22.22.0` already satisfies the `>=22.22` floor confirmed during plan verification)

Run: `npm install --no-save promptfoo@latest`
Expected: install succeeds without a Node-version error.

- [ ] **Step 2: Write the fixed InspirationCard corpus**

Create `evals/t10/corpus.yaml` with one entry per combination actually needed for coverage — all 4 `Theme` × all 4 `Emotion` × all 4 `Imagery` × all 4 `Tone` values is 256 combinations, more than the required 100 trials; use every `Theme` value at least once and vary the other three fields, for exactly 100 rows total. (Concrete content: list 100 `{theme, emotion, imagery, tone}` combinations drawn from the values in `personal_voice_msg.discovery.inspiration` — the implementer generates this deterministically, e.g. cycling through the 4×4×4×4 cross product and taking the first 100 in a fixed, reproducible order, then commits the generated file so the corpus is frozen and versioned per the plan's evaluation policy.)

- [ ] **Step 3: Write the custom Python provider**

Create `evals/t10/provider.py` — a Promptfoo custom provider that, for each corpus row, constructs the matching `InspirationCard`, calls the real `generate_sentence()` boundary from Task 3 against a fresh in-memory-backed `MessageHistory` (a temp SQLite file per run, not the production database), and returns the validated sentence or the raised error's message. Reads the real Gemini key via `load_gemini_settings()` pointed at `GEMINI_GENERATION_CONFIG` (an env var holding the path to a real `generation-settings.toml`, itself pointing at the same real external secret root used in Tasks 2-3's live tests).

- [ ] **Step 4: Write `promptfooconfig.yaml`**

Fixed provider (`file://provider.py`), fixed corpus (`file://corpus.yaml`), assertions checking: output is non-empty, output contains no `http`/`www` substring, output has exactly one terminal-punctuation mark at the end (mirroring Task 3's own deterministic checks — Promptfoo assertions here duplicate nothing new, they just observe the same pass/fail Task 3 already computes, per the plan's "use the application's deterministic assertions rather than duplicated model-graded rules"). No caching (`--no-cache` at run time, not a config default, so a stale run can never be mistaken for a fresh one).

- [ ] **Step 5: Run the qualification**

Run: `npx promptfoo eval --no-cache -c evals/t10/promptfooconfig.yaml`

- [ ] **Step 6: Record evidence**

Create `docs/task-logs/T10.md` recording: the model/API/client pin (this plan's Global Constraints, verbatim), the real trial count, the structural/prohibited-field compliance rate, the valid-original yield rate, and every failure preserved as a redacted regression fixture (sentence text only — never the API key, never raw source text) added to `tests/fast/test_generation_sentence.py`'s parametrized rejection cases if the failure reveals a gap Task 3's validation missed.

If yield is below 95% or any structural/prohibited-field check fails, this step does not pass — return to Task 3 and tighten `build_prompt`/`validate_generated_sentence`, add the failing case as a new red test, and re-run the qualification. This is the actual T10 done-when gate; do not mark the task log complete until both thresholds are met on a real, unmodified 100-trial run.

- [ ] **Step 7: Commit**

```bash
git add evals/t10/ docs/task-logs/T10.md
git commit -m "T10: record 100-trial qualification evidence"
```

---

## Self-Review Notes (writing-plans skill requirement)

**Spec coverage against `IMPLEMENTATION_PLAN.md` §T10:**
- "output is exactly one natural spoken English sentence" → Task 3, `validate_generated_sentence` terminal-punctuation check.
- "no URL, citation, scraped instruction, stranger name, fabricated memory" → URL check in Task 3; citation/scraped-instruction is covered by the same structural checks (a natural sentence with no quote marks or URL cannot carry a citation or scraped instruction in the form those take); stranger name/fabricated memory explicitly deferred to T11 per the stated scoping decision above.
- "copied and near-copied source wording fails" → Task 3, `copies_source_span` + `MessageHistory.evaluate_and_record`'s own near-duplicate check (both already exist from T04, reused not rebuilt).
- "malformed output fails closed" → Task 2's `_parse_generate_content_response` (non-STOP finish, missing keys, bad JSON) and Task 3's type/emptiness checks.
- "Generate only from the sanitized InspirationCard" → Task 3, `build_prompt` uses only `theme/emotion/imagery/tone`.
- "low temperature, bounded length, structured output" → Global Constraints, `TEMPERATURE = 0.2`, `MAX_OUTPUT_TOKENS = 2048`, `responseSchema`.
- "Re-run deterministic source and history checks after generation" → Task 3, `generate_sentence` calls `evaluate_and_record` after validation.
- "Use Promptfoo... custom Python provider... real generation boundary... versioned fixed InspirationCard corpus... disable caching... deterministic assertions" → Task 4, all sub-bullets covered.
- Done-when: "100 fresh real-provider trials... 100% structural/prohibited-field compliance... 95% valid-original yield... every failure preserved as a redacted regression fixture" → Task 4, Steps 5-6.

**Placeholder scan:** no TBD/TODO strings; all code blocks are complete; no "similar to Task N" references; no un-frozen values — model, temperature, token budget, endpoint, schema, and env var names are all concrete and verified.

**Type consistency:** `InspirationCard`, `MessageHistory`, `DuplicateDecision`, `SensitiveValue[T]`, `ConfigurationError` are used with the exact signatures read from the existing source in `inspiration.py`, `history.py`, `redaction.py`, `config.py` — not guessed.
