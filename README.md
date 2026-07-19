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
