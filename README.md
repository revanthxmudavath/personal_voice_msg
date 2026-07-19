# Personal Voice Message Assistant

Cloud-hosted generation and delivery of one original romantic voice note per
Pacific calendar date. Production behavior is implemented incrementally under
the task plan; T01 contains repository tooling only.

## Development

Python 3.12 and `uv` are required. `uv sync --locked` creates and maintains the
project-local `.venv`; all commands below execute inside that environment.

```powershell
uv sync --locked
uv run pytest -m fast
uv run ruff check .
uv run mypy src
uv run python scripts/repository_policy.py all --root .
```

Integration, security, live, and end-to-end suites are opt-in. Tests must use
real implementations and protocol endpoints, never mocks.

## Configuration boundary

The non-secret TOML configuration contains only a runtime profile, a secret
root, and relative secret-file names. Recipient data, WAHA tokens, the voice
embedding, and WhatsApp session data must be provisioned outside the repository
as root-owned files readable only by the intended service identity. They are
never accepted as command-line values.

Development, staging, and production recipients are profile-bound. A process
fails closed if settings are missing or unknown, a secret path escapes its
configured root, or recipient data belongs to a different profile.

## SQLite state boundary

SQLite is the operational source of truth. Migrations and all state changes use
real file-backed databases with foreign keys enabled. Content moves through
`discovered`, `validated`, `approved`, and `queued`; atomic reservation then
drives `reserved`, `audio_ready`, `sending`, and a terminal delivery state.

Reservations use an opaque recipient key plus Pacific date and begin with an
immediate write transaction. This prevents competing workers from reserving a
second message for the same recipient/date without storing a phone number.

## Message history and deduplication

Message text is normalized with Unicode compatibility folding, case folding,
punctuation removal, and whitespace collapsing. Exact variants are found by a
SHA-256 hash. An external-content SQLite FTS5 index supports lexical history
search without storing a second copy of each sentence. RapidFuzz scores the
complete stored history and rejects token-sorted scores at or above `84.0`.

The threshold rejects every known duplicate in the T04 corpus. Its documented
conservative tradeoff is that distinct sentences using nearly the same words in
a different order can also be rejected. Six consecutive normalized words copied
from transient source text are always rejected by a separate deterministic
check. Invisible Unicode format characters cannot bypass that comparison, and
stored message text is immutable after its hash is recorded. Source passages
and duplicate-comparison text are never written to logs.
