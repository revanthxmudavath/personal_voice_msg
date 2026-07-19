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
