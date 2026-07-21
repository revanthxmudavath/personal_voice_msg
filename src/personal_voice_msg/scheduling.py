from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
TRIGGER_WINDOW = timedelta(minutes=1)


class ScheduleKind(StrEnum):
    WEEKLY_DISCOVERY = "weekly_discovery"
    DAILY_PREPARE = "daily_prepare"
    DAILY_SEND = "daily_send"


class TriggerStatus(StrEnum):
    NOT_DUE = "not_due"
    DUE = "due"
    MISSED = "missed"


@dataclass(frozen=True, slots=True)
class ScheduledTrigger:
    kind: ScheduleKind
    pacific_date: date
    scheduled_at: datetime
    cutoff_at: datetime


def _scheduled_trigger(
    kind: ScheduleKind,
    pacific_date: date,
    wall_time: time,
) -> ScheduledTrigger:
    local_time = datetime.combine(pacific_date, wall_time, tzinfo=PACIFIC)
    scheduled_at = local_time.astimezone(UTC)
    return ScheduledTrigger(
        kind=kind,
        pacific_date=pacific_date,
        scheduled_at=scheduled_at,
        cutoff_at=scheduled_at + TRIGGER_WINDOW,
    )


def planned_triggers_for_date(pacific_date: date) -> tuple[ScheduledTrigger, ...]:
    if not isinstance(pacific_date, date) or isinstance(pacific_date, datetime):
        raise ValueError("Pacific date must be a date without a time")

    triggers: list[ScheduledTrigger] = []
    if pacific_date.weekday() == 0:
        triggers.append(
            _scheduled_trigger(
                ScheduleKind.WEEKLY_DISCOVERY,
                pacific_date,
                time(0, 0),
            )
        )
    triggers.extend(
        (
            _scheduled_trigger(
                ScheduleKind.DAILY_PREPARE,
                pacific_date,
                time(6, 50),
            ),
            _scheduled_trigger(
                ScheduleKind.DAILY_SEND,
                pacific_date,
                time(7, 0),
            ),
        )
    )
    return tuple(triggers)


def classify_trigger(trigger: ScheduledTrigger, now: datetime) -> TriggerStatus:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("current time must be timezone-aware")
    instant = now.astimezone(UTC)
    if instant < trigger.scheduled_at:
        return TriggerStatus.NOT_DUE
    if instant < trigger.cutoff_at:
        return TriggerStatus.DUE
    return TriggerStatus.MISSED
