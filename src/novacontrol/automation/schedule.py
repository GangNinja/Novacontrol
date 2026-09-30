"""WHEN an automation runs, as data rather than a hidden timer.

An automation's schedule is the part most likely to be wrong in a way nobody
notices until the wrong hour, so it is a value: a :class:`Schedule` says what
kind of repetition it is, when it was requested, and nothing else. The engine
asks it :meth:`Schedule.next_after` and stores the answer, so "what runs next"
is readable state rather than a countdown somebody has to reason about.

Two rules keep this honest:

  * **a schedule is computed, never guessed.** ``parse_schedule`` returns
    ``None`` when the sentence states no concrete time or interval, because a
    request that merely mentions a schedule is not a request to create one —
    creation still travels the pipeline (intent → decision → plan → permission →
    execution), and a parser that invented a default time would be deciding for
    the user at the wrong layer.
  * **the next occurrence is strictly in the future.** A machine that was off
    over a daily job's hour does not get a burst of catch-up runs when it wakes;
    the schedule advances to the next occurrence after ``now``, and the skipped
    ones are simply the past.

Times are read on the clock the caller passes in — the machine's local clock in
production, a fixed one in tests — and ``once`` instants are stored as the
timezone-aware datetime they were computed as.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Any

#: Weekday numbers as ``datetime.weekday()`` counts them (Monday is 0).
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

#: Named hours a phrase may name instead of a number.
_NAMED_HOURS: dict[str, tuple[int, int]] = {
    "morning": (9, 0),
    "noon": (12, 0),
    "afternoon": (15, 0),
    "evening": (18, 0),
    "night": (21, 0),
    "tonight": (21, 0),
}

_UNIT_SECONDS: dict[str, float] = {
    "second": 1.0,
    "seconds": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "minute": 60.0,
    "minutes": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "day": 86400.0,
    "days": 86400.0,
    "week": 604800.0,
    "weeks": 604800.0,
}

#: The smallest interval a recurring automation may be given. A schedule that
#: fires many times a second is not an automation, it is a busy loop — and the
#: engine would be the thing melting.
MIN_INTERVAL_SECONDS = 1.0

_EVERY_UNIT = re.compile(
    r"\bevery\s+(\d+(?:\.\d+)?)\s*(" + "|".join(_UNIT_SECONDS) + r")\b", re.I
)
_IN_UNIT = re.compile(
    r"\bin\s+(\d+(?:\.\d+)?)\s*(" + "|".join(_UNIT_SECONDS) + r")\b", re.I
)
_EVERY_WEEKDAY = re.compile(
    r"\bevery\s+(" + "|".join(WEEKDAY_NAMES) + r")\b", re.I
)
_EVERY_NAMED = re.compile(
    r"\b(?:every|each)\s+(morning|noon|afternoon|evening|night)\b", re.I
)
_DAILY = re.compile(r"\b(every\s+day|daily|each\s+day)\b", re.I)
_TOMORROW = re.compile(r"\btomorrow\b", re.I)
_TONIGHT = re.compile(r"\btonight\b", re.I)
_AT_TIME = re.compile(
    r"\b(?:at|by)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b", re.I
)

#: The phrases that SAY a schedule, removed by :func:`strip_schedule`. Built from
#: the same vocabulary as the parser above: a phrase the parser reads as a
#: schedule is a phrase that must not travel with the stored request, or the
#: stored request would be read as a request to schedule something all over
#: again every time it ran.
_SCHEDULE_PHRASES: tuple[str, ...] = (
    r"\b(?:please\s+)?remind\s+me\b",
    r"\b(?:every|each)\s+\d+(?:\.\d+)?\s*(?:" + "|".join(_UNIT_SECONDS) + r")\b",
    r"\bin\s+\d+(?:\.\d+)?\s*(?:" + "|".join(_UNIT_SECONDS) + r")\b",
    r"\b(?:every|each)\s+(?:" + "|".join(WEEKDAY_NAMES) + r")\b",
    r"\b(?:every|each)\s+(?:morning|noon|afternoon|evening|night)\b",
    r"\b(?:every\s+day|each\s+day|daily)\b",
    r"\btomorrow\b",
    r"\btonight\b",
    r"\b(?:at|by)\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?\b",
)

#: Words left dangling once the schedule phrase is gone. Stripped from the ends
#: only: a "to" in the middle of a request belongs to the request.
_GLUE = re.compile(
    r"^(?:,|;|\.|:|-|\bto\b|\band\b|\bthen\b|\bthat\b|\bit\b|\bme\b|\bplease\b|"
    r"\bfor\b|\bdo\b|\bso\b)\s*",
    re.I,
)


class ScheduleKind(StrEnum):
    """How an automation repeats."""

    ONCE = "once"
    INTERVAL = "interval"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass(frozen=True, slots=True)
class Schedule:
    """One schedule: a single instant, a period, or a wall-clock recurrence.

    Only the fields the kind uses may be set, and the constructor refuses the
    combinations that would make the next run ambiguous rather than picking one
    of them silently.
    """

    kind: ScheduleKind
    #: ONCE only: the instant to run at (timezone-aware).
    at: datetime | None = None
    #: INTERVAL only: seconds between runs.
    every_seconds: float | None = None
    #: DAILY/WEEKLY: the wall-clock time of day to run at.
    time_of_day: time | None = None
    #: WEEKLY only: the weekday (Monday is 0).
    weekday: int | None = None

    def __post_init__(self) -> None:
        if self.kind is ScheduleKind.ONCE:
            if self.at is None:
                raise ValueError("A one-time schedule needs the instant to run at.")
            if self.at.tzinfo is None:
                raise ValueError("A one-time schedule needs a timezone-aware instant.")
        elif self.kind is ScheduleKind.INTERVAL:
            if self.every_seconds is None:
                raise ValueError("A recurring schedule needs the interval in seconds.")
            if self.every_seconds < MIN_INTERVAL_SECONDS:
                raise ValueError(
                    f"A recurring schedule must be at least {MIN_INTERVAL_SECONDS:g} "
                    "second apart."
                )
        elif self.kind in (ScheduleKind.DAILY, ScheduleKind.WEEKLY):
            if self.time_of_day is None:
                raise ValueError("A calendar schedule needs the time of day to run at.")
        if self.kind is ScheduleKind.WEEKLY and self.weekday is None:
            raise ValueError("A weekly schedule needs the weekday to run on.")
        if self.kind is not ScheduleKind.WEEKLY and self.weekday is not None:
            raise ValueError("Only a weekly schedule names a weekday.")

    # -- the one question the engine asks --------------------------------------

    def next_after(
        self, now: datetime, *, previous: datetime | None = None
    ) -> datetime | None:
        """The first occurrence strictly after ``now``, or ``None`` when done.

        ``None`` means exactly one thing: this schedule has no further
        occurrences (a one-time task whose instant has passed). A recurrence
        always has a next occurrence, which is why it can never be confused with
        a cancelled or disabled task.
        """
        if now.tzinfo is None:
            raise ValueError("Schedules are compared against a timezone-aware clock.")
        if self.kind is ScheduleKind.ONCE:
            assert self.at is not None
            return self.at if self.at > now else None
        if self.kind is ScheduleKind.INTERVAL:
            assert self.every_seconds is not None
            interval = timedelta(seconds=self.every_seconds)
            base = previous if previous is not None and previous.tzinfo is not None else now
            candidate = base + interval
            # Compressed catch-up, not a burst: a machine that was asleep over
            # several intervals gets ONE next run, at the first occurrence
            # strictly after now.
            if candidate <= now:
                missed = (now - candidate) // interval + 1
                candidate = candidate + interval * missed
            return candidate
        if self.kind is ScheduleKind.DAILY:
            assert self.time_of_day is not None
            candidate = datetime.combine(
                now.date(), self.time_of_day, tzinfo=now.tzinfo
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
        assert self.time_of_day is not None and self.weekday is not None
        candidate = datetime.combine(now.date(), self.time_of_day, tzinfo=now.tzinfo)
        ahead = (self.weekday - now.weekday()) % 7
        candidate += timedelta(days=ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    # -- reporting -------------------------------------------------------------

    def describe(self) -> str:
        """The schedule in words, for a person reading a task list."""
        if self.kind is ScheduleKind.ONCE:
            assert self.at is not None
            return f"once at {self.at.isoformat(timespec='minutes')}"
        if self.kind is ScheduleKind.INTERVAL:
            assert self.every_seconds is not None
            return f"every {_human_interval(self.every_seconds)}"
        assert self.time_of_day is not None
        clock = self.time_of_day.strftime("%H:%M")
        if self.kind is ScheduleKind.DAILY:
            return f"every day at {clock}"
        assert self.weekday is not None
        return f"every {WEEKDAY_NAMES[self.weekday]} at {clock}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "at": self.at.isoformat() if self.at is not None else None,
            "every_seconds": self.every_seconds,
            "time_of_day": self.time_of_day.strftime("%H:%M") if self.time_of_day else None,
            "weekday": self.weekday,
            "description": self.describe(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Schedule:
        raw_time = payload.get("time_of_day")
        parsed_time: time | None = None
        if raw_time:
            hour, _, minute = str(raw_time).partition(":")
            parsed_time = time(int(hour), int(minute or 0))
        raw_at = payload.get("at")
        weekday = payload.get("weekday")
        return cls(
            kind=ScheduleKind(str(payload["kind"])),
            at=datetime.fromisoformat(str(raw_at)) if raw_at else None,
            every_seconds=(
                float(payload["every_seconds"])
                if payload.get("every_seconds") is not None
                else None
            ),
            time_of_day=parsed_time,
            weekday=int(weekday) if weekday is not None else None,
        )


def _human_interval(seconds: float) -> str:
    for unit, size in (("day", 86400.0), ("hour", 3600.0), ("minute", 60.0)):
        if seconds % size == 0 and seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds:g} seconds"


def _parse_clock(text: str) -> time | None:
    """The wall-clock time a phrase names, or ``None`` when it names none."""
    named = _NAMED_HOURS.get(text.strip().lower())
    if named is not None:
        return time(named[0], named[1])
    match = _AT_TIME.search(text)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").lower()
    if hour > 23 or minute > 59:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return time(hour, minute)


def strip_schedule(text: str) -> str:
    """The request a sentence is ABOUT, with the scheduling phrase removed.

    An automation stores the request it will run later, and running that request
    goes through the same pipeline as a spoken one. So the schedule must not
    travel with it: *"remind me at 6 PM to push my project"* stores "push my
    project", because a stored copy that still said "at 6 PM" would be read as a
    request to schedule something, and create a second automation every time it
    ran.

    Falls back to the original text when stripping would leave nothing — an
    empty request is worse than one carrying a phrase that was already consumed.
    """
    cleaned = str(text or "")
    for phrase in _SCHEDULE_PHRASES:
        cleaned = re.sub(phrase, " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    while True:
        trimmed = _GLUE.sub("", cleaned).strip()
        if trimmed == cleaned:
            break
        cleaned = trimmed
    cleaned = cleaned.strip(" ,;.:-")
    return cleaned or str(text or "").strip()


def parse_schedule(text: str, *, now: datetime | None = None) -> Schedule | None:
    """The schedule a sentence states, or ``None`` when it states none.

    Deliberately narrow. A sentence that talks ABOUT schedules without naming a
    time ("when should I run my backup?") parses to ``None``, so nothing is
    scheduled by accident, and a sentence that names one ("every Monday at 9",
    "in 10 minutes") yields a value the pipeline can carry.
    """
    if not text or not text.strip():
        return None
    clock = now or datetime.now(UTC).astimezone()
    lowered = text.lower()

    interval = _EVERY_UNIT.search(text)
    if interval is not None:
        seconds = float(interval.group(1)) * _UNIT_SECONDS[interval.group(2).lower()]
        if seconds < MIN_INTERVAL_SECONDS:
            return None
        return Schedule(kind=ScheduleKind.INTERVAL, every_seconds=seconds)

    weekday = _EVERY_WEEKDAY.search(text)
    if weekday is not None:
        named = weekday.group(1).lower()
        return Schedule(
            kind=ScheduleKind.WEEKLY,
            weekday=WEEKDAY_NAMES.index(named),
            time_of_day=_parse_clock(text) or time(9, 0),
        )

    if _DAILY.search(text):
        return Schedule(
            kind=ScheduleKind.DAILY,
            time_of_day=_parse_clock(text) or time(9, 0),
        )

    named_recurring = _EVERY_NAMED.search(text)
    if named_recurring is not None:
        return Schedule(
            kind=ScheduleKind.DAILY,
            time_of_day=_parse_clock(named_recurring.group(1)),
        )

    relative = _IN_UNIT.search(text)
    if relative is not None:
        seconds = float(relative.group(1)) * _UNIT_SECONDS[relative.group(2).lower()]
        if seconds < MIN_INTERVAL_SECONDS:
            return None
        return Schedule(
            kind=ScheduleKind.ONCE, at=clock + timedelta(seconds=seconds)
        )

    if _TOMORROW.search(text):
        named = _parse_clock(text)
        if named is None:
            return None
        candidate = datetime.combine(clock.date(), named, tzinfo=clock.tzinfo)
        candidate += timedelta(days=1)
        return Schedule(kind=ScheduleKind.ONCE, at=candidate)

    if _TONIGHT.search(lowered):
        named = _NAMED_HOURS["tonight"]
        candidate = datetime.combine(
            clock.date(), time(named[0], named[1]), tzinfo=clock.tzinfo
        )
        return Schedule(kind=ScheduleKind.ONCE, at=candidate)

    named = _parse_clock(text)
    if named is not None:
        candidate = datetime.combine(clock.date(), named, tzinfo=clock.tzinfo)
        if candidate <= clock:
            # "at 6 PM" said at 19:00 is tomorrow's 18:00, and saying so is
            # better than running immediately or refusing the request.
            candidate += timedelta(days=1)
        return Schedule(kind=ScheduleKind.ONCE, at=candidate)
    return None
