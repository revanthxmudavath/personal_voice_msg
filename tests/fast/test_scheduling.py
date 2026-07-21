from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from personal_voice_msg.scheduling import (
    ScheduledTrigger,
    ScheduleKind,
    TriggerStatus,
    classify_trigger,
    planned_triggers_for_date,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def trigger_for(day: date, kind: ScheduleKind) -> ScheduledTrigger:
    matches = [
        trigger
        for trigger in planned_triggers_for_date(day)
        if trigger.kind is kind
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.fast
@pytest.mark.parametrize(
    ("day", "prepare_at", "send_at"),
    [
        (
            date(2026, 3, 7),
            datetime(2026, 3, 7, 14, 50, tzinfo=UTC),
            datetime(2026, 3, 7, 15, 0, tzinfo=UTC),
        ),
        (
            date(2026, 3, 8),
            datetime(2026, 3, 8, 13, 50, tzinfo=UTC),
            datetime(2026, 3, 8, 14, 0, tzinfo=UTC),
        ),
        (
            date(2026, 10, 31),
            datetime(2026, 10, 31, 13, 50, tzinfo=UTC),
            datetime(2026, 10, 31, 14, 0, tzinfo=UTC),
        ),
        (
            date(2026, 11, 1),
            datetime(2026, 11, 1, 14, 50, tzinfo=UTC),
            datetime(2026, 11, 1, 15, 0, tzinfo=UTC),
        ),
    ],
    ids=["before-spring", "spring", "before-autumn", "autumn"],
)
def test_daily_triggers_stay_at_pacific_wall_time_across_dst(
    day: date,
    prepare_at: datetime,
    send_at: datetime,
) -> None:
    prepare = trigger_for(day, ScheduleKind.DAILY_PREPARE)
    send = trigger_for(day, ScheduleKind.DAILY_SEND)

    assert prepare.scheduled_at == prepare_at
    assert send.scheduled_at == send_at
    assert prepare.scheduled_at.astimezone(PACIFIC).time().isoformat() == "06:50:00"
    assert send.scheduled_at.astimezone(PACIFIC).time().isoformat() == "07:00:00"


@pytest.mark.fast
def test_same_instant_in_different_server_offsets_has_one_status() -> None:
    trigger = trigger_for(date(2026, 7, 20), ScheduleKind.DAILY_SEND)
    same_instant = (
        datetime(2026, 7, 20, 14, 0, 30, tzinfo=UTC),
        datetime(
            2026,
            7,
            20,
            10,
            0,
            30,
            tzinfo=timezone(timedelta(hours=-4)),
        ),
        datetime(
            2026,
            7,
            20,
            19,
            30,
            30,
            tzinfo=timezone(timedelta(hours=5, minutes=30)),
        ),
    )

    assert {classify_trigger(trigger, now) for now in same_instant} == {
        TriggerStatus.DUE
    }


@pytest.mark.fast
def test_classification_rejects_a_naive_timestamp() -> None:
    trigger = trigger_for(date(2026, 7, 20), ScheduleKind.DAILY_SEND)

    with pytest.raises(ValueError, match="timezone-aware"):
        classify_trigger(trigger, datetime(2026, 7, 20, 7, 0))


@pytest.mark.fast
@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(microseconds=-1), TriggerStatus.NOT_DUE),
        (timedelta(0), TriggerStatus.DUE),
        (timedelta(seconds=59, microseconds=999_999), TriggerStatus.DUE),
        (timedelta(minutes=1), TriggerStatus.MISSED),
        (timedelta(hours=1), TriggerStatus.MISSED),
    ],
)
def test_trigger_has_one_minute_due_window_then_is_missed(
    offset: timedelta,
    expected: TriggerStatus,
) -> None:
    trigger = trigger_for(date(2026, 7, 20), ScheduleKind.DAILY_SEND)

    assert classify_trigger(trigger, trigger.scheduled_at + offset) is expected


@pytest.mark.fast
def test_weekly_discovery_is_monday_midnight_pacific() -> None:
    sunday = date(2026, 7, 19)
    monday = date(2026, 7, 20)

    assert all(
        trigger.kind is not ScheduleKind.WEEKLY_DISCOVERY
        for trigger in planned_triggers_for_date(sunday)
    )
    discovery = trigger_for(monday, ScheduleKind.WEEKLY_DISCOVERY)
    assert discovery.pacific_date == monday
    assert discovery.scheduled_at == datetime(2026, 7, 20, 7, 0, tzinfo=UTC)
    assert discovery.scheduled_at.astimezone(PACIFIC).time().isoformat() == "00:00:00"


@pytest.mark.fast
@pytest.mark.parametrize(
    ("day", "scheduled_at"),
    [
        (date(2026, 3, 2), datetime(2026, 3, 2, 8, 0, tzinfo=UTC)),
        (date(2026, 3, 9), datetime(2026, 3, 9, 7, 0, tzinfo=UTC)),
        (date(2026, 10, 26), datetime(2026, 10, 26, 7, 0, tzinfo=UTC)),
        (date(2026, 11, 2), datetime(2026, 11, 2, 8, 0, tzinfo=UTC)),
    ],
    ids=["spring-before", "spring-after", "autumn-before", "autumn-after"],
)
def test_weekly_discovery_follows_pacific_dst(
    day: date,
    scheduled_at: datetime,
) -> None:
    assert trigger_for(day, ScheduleKind.WEEKLY_DISCOVERY).scheduled_at == scheduled_at


@pytest.mark.fast
def test_leap_year_has_exactly_one_daily_trigger_per_pacific_date() -> None:
    first_day = date(2028, 1, 1)
    days = [first_day + timedelta(days=offset) for offset in range(366)]
    triggers = [
        trigger
        for day in days
        for trigger in planned_triggers_for_date(day)
    ]
    prepare = [
        trigger for trigger in triggers if trigger.kind is ScheduleKind.DAILY_PREPARE
    ]
    send = [trigger for trigger in triggers if trigger.kind is ScheduleKind.DAILY_SEND]
    discovery = [
        trigger
        for trigger in triggers
        if trigger.kind is ScheduleKind.WEEKLY_DISCOVERY
    ]

    assert len(prepare) == 366
    assert len(send) == 366
    assert len(discovery) == 52
    assert {trigger.pacific_date for trigger in prepare} == set(days)
    assert {trigger.pacific_date for trigger in send} == set(days)
    assert all(trigger.pacific_date.weekday() == 0 for trigger in discovery)
    assert len(
        {(trigger.kind, trigger.pacific_date) for trigger in triggers}
    ) == len(triggers)
    assert all(
        trigger.scheduled_at.astimezone(PACIFIC).time().isoformat() == "06:50:00"
        for trigger in prepare
    )
    assert all(
        trigger.scheduled_at.astimezone(PACIFIC).time().isoformat() == "07:00:00"
        for trigger in send
    )
