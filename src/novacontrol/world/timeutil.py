"""Timestamp arithmetic, kept in one place and honest about what it cannot read.

Three rules, all of them consequences of the phase's stance on unmeasured data:

**An unparseable timestamp returns ``None``.** Never zero, never "now". A caller
that gets ``None`` knows it must not rank that record against another; a caller
that got a fabricated value would silently mis-order a history.

**A naive timestamp is read as UTC.** Nearly every source in this build stamps
ISO-8601 with an offset, but a hand-written observation often does not, and
treating a naive stamp as local time would make two machines disagree about the
same file. UTC is the assumption that at least travels.

**Ordering across DIFFERENT clock domains is not attempted.** ``is_newer`` takes
the clock name into account: a record from another clock is not "older" or
"newer", it is incomparable, and saying so is why ``Observation.clock`` exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = [
    "is_newer",
    "parse_timestamp",
    "seconds_between",
    "timestamps_in_order",
]


def parse_timestamp(value: object) -> datetime | None:
    """An aware datetime, or ``None`` when the text is not a timestamp.

    Accepts what the build actually emits (``datetime.now(UTC).isoformat()``) and
    the common hand-written forms — a bare date, a trailing ``Z``, a space
    instead of ``T``, and a fractional second of any length.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    candidate = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(candidate, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        # Naive input is UTC, not local time: an assumption that at least
        # travels between machines, and one the module docstring states.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def seconds_between(earlier: object, later: object) -> float | None:
    """Seconds from one timestamp to another, or ``None`` if either is unreadable.

    Negative when the second is EARLIER than the first — the caller decides
    whether that is out-of-order data (it usually is) or an intentional
    comparison of two historical points.
    """
    first = parse_timestamp(earlier)
    second = parse_timestamp(later)
    if first is None or second is None:
        return None
    return (second - first).total_seconds()


def is_newer(
    candidate: object,
    reference: object,
    *,
    clock: str = "",
    other_clock: str = "",
) -> bool | None:
    """Whether ``candidate`` is strictly later than ``reference``.

    ``None`` means "cannot tell" — an unreadable timestamp, or two records from
    different clock domains, which are not comparable no matter how they look.
    The three-valued answer is the point: a caller that treats "cannot tell" as
    "no" would drop a fact; the one that treats it as "yes" would overwrite a
    newer one.
    """
    if clock and other_clock and clock != other_clock:
        return None
    first = parse_timestamp(candidate)
    second = parse_timestamp(reference)
    if first is None or second is None:
        return None
    return first > second


def timestamps_in_order(values: object) -> tuple[bool, tuple[str, ...]]:
    """Whether a sequence of timestamps is non-decreasing, plus the unreadable ones.

    ``timestamp and provenance`` travel together for a history query (§22D): the
    order is only claimed when every stamp could be read, and the ones that could
    not are returned so the caller can say so instead of guessing.
    """
    unreadable: list[str] = []
    previous: datetime | None = None
    ordered = True
    if not isinstance(values, (list, tuple)):
        return True, ()
    for item in values:
        parsed = parse_timestamp(item)
        if parsed is None:
            unreadable.append(str(item))
            continue
        if previous is not None and parsed < previous:
            ordered = False
        previous = parsed
    return ordered, tuple(unreadable)
