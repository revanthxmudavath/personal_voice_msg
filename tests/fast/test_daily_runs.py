from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from personal_voice_msg.database import Database, InvalidTransition

NOW = datetime(2026, 7, 20, 13, 50, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
PACIFIC_DATE = date(2026, 7, 20)
RECIPIENT = "recipient_staging_test"


def count_daily_runs(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM daily_runs").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.fast
def test_daily_run_claim_survives_database_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "daily-runs.sqlite3"
    database = Database(database_path)
    database.migrate()

    claimed = database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)

    assert claimed is not None
    assert claimed.recipient_key == RECIPIENT
    assert claimed.pacific_date == PACIFIC_DATE
    assert Database(database_path).get_daily_run(RECIPIENT, PACIFIC_DATE) == claimed
    assert count_daily_runs(database_path) == 1


@pytest.mark.fast
def test_daily_run_cannot_be_claimed_twice_before_during_or_after_completion(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "single-daily-run.sqlite3"
    database = Database(database_path)
    database.migrate()

    claimed = database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)
    assert claimed is not None
    assert database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW) is None
    assert (
        Database(database_path).claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)
        is None
    )

    completed = Database(database_path).complete_daily_run(
        claimed.run_id, COMPLETED_AT
    )

    assert completed.run_id == claimed.run_id
    assert completed.finished_at == COMPLETED_AT
    assert database.claim_daily_run(RECIPIENT, PACIFIC_DATE, COMPLETED_AT) is None
    assert count_daily_runs(database_path) == 1


@pytest.mark.fast
def test_two_workers_create_exactly_one_daily_run_claim(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrent-daily-run.sqlite3"
    Database(database_path).migrate()
    ready = Barrier(2)

    def claim() -> object | None:
        worker_database = Database(database_path)
        ready.wait(timeout=5)
        return worker_database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(claim) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert sum(result is not None for result in results) == 1
    assert count_daily_runs(database_path) == 1


@pytest.mark.fast
def test_daily_run_claims_are_independent_by_recipient_and_pacific_date(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "independent-daily-runs.sqlite3"
    database = Database(database_path)
    database.migrate()

    claims = (
        database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW),
        database.claim_daily_run(
            "recipient_production_test", PACIFIC_DATE, NOW
        ),
        database.claim_daily_run(
            RECIPIENT,
            date(2026, 7, 21),
            NOW + timedelta(days=1),
        ),
    )

    assert all(claim is not None for claim in claims)
    assert count_daily_runs(database_path) == 3


@pytest.mark.fast
@pytest.mark.parametrize(
    "now",
    [
        NOW - timedelta(microseconds=1),
        NOW + timedelta(minutes=10),
        NOW + timedelta(minutes=10, microseconds=1),
    ],
    ids=("before-prepare", "send-start", "after-prepare"),
)
def test_daily_run_claim_is_rejected_outside_the_prepare_window(
    tmp_path: Path,
    now: datetime,
) -> None:
    database_path = tmp_path / "outside-prepare.sqlite3"
    database = Database(database_path)
    database.migrate()

    with pytest.raises(ValueError, match="prepare window"):
        database.claim_daily_run(RECIPIENT, PACIFIC_DATE, now)

    assert count_daily_runs(database_path) == 0


@pytest.mark.fast
@pytest.mark.parametrize(
    "now",
    [
        NOW,
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=9, seconds=59, microseconds=999_999),
    ],
    ids=("prepare-start", "inside-prepare", "last-prepare-microsecond"),
)
def test_daily_run_claim_is_accepted_throughout_the_prepare_window(
    tmp_path: Path,
    now: datetime,
) -> None:
    database_path = tmp_path / "inside-prepare.sqlite3"
    database = Database(database_path)
    database.migrate()

    assert database.claim_daily_run(RECIPIENT, PACIFIC_DATE, now) is not None
    assert count_daily_runs(database_path) == 1


@pytest.mark.fast
def test_daily_run_claim_rejects_a_date_not_selected_by_the_pacific_instant(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "wrong-pacific-date.sqlite3"
    database = Database(database_path)
    database.migrate()

    with pytest.raises(ValueError, match="prepare window"):
        database.claim_daily_run(RECIPIENT, date(2026, 7, 21), NOW)

    assert count_daily_runs(database_path) == 0


@pytest.mark.fast
@pytest.mark.parametrize(
    "now",
    [
        NOW,
        NOW.astimezone(timezone(timedelta(hours=-4))),
        NOW.astimezone(timezone(timedelta(hours=5, minutes=30))),
    ],
)
def test_daily_run_claim_uses_the_instant_not_the_server_offset(
    tmp_path: Path,
    now: datetime,
) -> None:
    database_path = tmp_path / "server-offset.sqlite3"
    database = Database(database_path)
    database.migrate()

    assert database.claim_daily_run(RECIPIENT, PACIFIC_DATE, now) is not None
    assert count_daily_runs(database_path) == 1


@pytest.mark.fast
@pytest.mark.parametrize(
    ("recipient_key", "pacific_date", "now"),
    [
        ("+14155550123", PACIFIC_DATE, NOW),
        ("14155550123", PACIFIC_DATE, NOW),
        (
            RECIPIENT,
            datetime(2026, 7, 20, 7, 0, tzinfo=UTC),
            NOW,
        ),
        (RECIPIENT, PACIFIC_DATE, datetime(2026, 7, 20, 13, 50)),
    ],
    ids=(
        "raw-phone-number",
        "digits-only-phone-number",
        "datetime-instead-of-date",
        "naive-timestamp",
    ),
)
def test_invalid_daily_run_claim_inputs_leave_no_row(
    tmp_path: Path,
    recipient_key: str,
    pacific_date: date,
    now: datetime,
) -> None:
    database_path = tmp_path / "invalid-daily-run.sqlite3"
    database = Database(database_path)
    database.migrate()

    with pytest.raises(ValueError):
        database.claim_daily_run(recipient_key, pacific_date, now)

    assert count_daily_runs(database_path) == 0


@pytest.mark.fast
def test_daily_run_completion_is_one_way_and_survives_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "completed-daily-run.sqlite3"
    database = Database(database_path)
    database.migrate()
    claimed = database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)
    assert claimed is not None

    completed = database.complete_daily_run(claimed.run_id, COMPLETED_AT)

    assert Database(database_path).get_daily_run(RECIPIENT, PACIFIC_DATE) == completed
    with pytest.raises(InvalidTransition):
        Database(database_path).complete_daily_run(claimed.run_id, COMPLETED_AT)
    assert count_daily_runs(database_path) == 1


@pytest.mark.fast
def test_restart_resumes_and_completes_the_same_claimed_daily_run(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "resumed-daily-run.sqlite3"
    database = Database(database_path)
    database.migrate()
    claimed = database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)
    assert claimed is not None

    restarted = Database(database_path)
    resumed = restarted.get_daily_run(RECIPIENT, PACIFIC_DATE)
    assert resumed == claimed

    completed = restarted.complete_daily_run(resumed.run_id, COMPLETED_AT)

    assert completed.run_id == claimed.run_id
    assert completed.finished_at == COMPLETED_AT
    assert count_daily_runs(database_path) == 1


@pytest.mark.fast
def test_naive_completion_timestamp_leaves_claim_unfinished(tmp_path: Path) -> None:
    database_path = tmp_path / "invalid-completion.sqlite3"
    database = Database(database_path)
    database.migrate()
    claimed = database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)
    assert claimed is not None

    with pytest.raises(ValueError):
        database.complete_daily_run(
            claimed.run_id,
            datetime(2026, 7, 20, 14, 0),
        )

    assert Database(database_path).get_daily_run(RECIPIENT, PACIFIC_DATE) == claimed


@pytest.mark.fast
def test_completion_before_daily_run_start_leaves_claim_unfinished(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "early-completion.sqlite3"
    database = Database(database_path)
    database.migrate()
    claimed = database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)
    assert claimed is not None

    with pytest.raises(ValueError, match="before.*start"):
        database.complete_daily_run(
            claimed.run_id,
            NOW - timedelta(microseconds=1),
        )

    assert Database(database_path).get_daily_run(RECIPIENT, PACIFIC_DATE) == claimed


@pytest.mark.fast
@pytest.mark.parametrize(
    "completed_at",
    [NOW, NOW + timedelta(microseconds=1)],
    ids=("same-instant", "later"),
)
def test_completion_at_or_after_daily_run_start_is_permitted(
    tmp_path: Path,
    completed_at: datetime,
) -> None:
    database_path = tmp_path / "valid-completion.sqlite3"
    database = Database(database_path)
    database.migrate()
    claimed = database.claim_daily_run(RECIPIENT, PACIFIC_DATE, NOW)
    assert claimed is not None

    completed = database.complete_daily_run(claimed.run_id, completed_at)

    assert completed.started_at == NOW
    assert completed.finished_at == completed_at
