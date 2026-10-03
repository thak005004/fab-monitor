"""Simulated clock. Every timestamp in the system comes from here (spec §2).

Never read the wall clock anywhere in this project.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_START = datetime(2026, 1, 5, 6, 0, 0, tzinfo=timezone.utc)
DEFAULT_TICK_LENGTH = timedelta(minutes=1)


class Clock:
    def __init__(
        self,
        start_time: datetime = DEFAULT_START,
        tick_length: timedelta = DEFAULT_TICK_LENGTH,
        tick: int = 0,
    ) -> None:
        if start_time.tzinfo is None:
            raise ValueError("start_time must be timezone-aware")
        if tick_length <= timedelta(0):
            raise ValueError("tick_length must be positive")
        if tick_length % timedelta(seconds=1):
            raise ValueError("tick_length must be whole seconds (timestamps have 1 s resolution)")
        if tick < 0:
            raise ValueError("tick must be >= 0")
        self.start_time = start_time
        self.tick_length = tick_length
        self._tick = tick

    @property
    def tick(self) -> int:
        return self._tick

    def at(self, tick: int) -> datetime:
        """The timestamp of any tick, past or future."""
        return self.start_time + tick * self.tick_length

    def now(self) -> datetime:
        return self.at(self._tick)

    def advance(self, n: int = 1) -> datetime:
        if n < 1:
            raise ValueError("can only advance forward")
        self._tick += n
        return self.now()

    def iso_at(self, tick: int) -> str:
        return to_iso(self.at(tick))

    def now_iso(self) -> str:
        return to_iso(self.now())

    # Tick arithmetic on stored timestamps lives here so no other module
    # does its own timestamp math or formatting.

    def shift_iso(self, ts: str, ticks: int) -> str:
        return to_iso(parse_iso(ts) + ticks * self.tick_length)

    def ticks_between(self, earlier: str, later: str) -> float:
        return (parse_iso(later) - parse_iso(earlier)) / self.tick_length

    def tick_of(self, ts: str) -> int:
        ticks = self.ticks_between(to_iso(self.start_time), ts)
        if ticks != int(ticks):
            raise ValueError(f"{ts} is not on a tick boundary")
        return int(ticks)


def to_iso(dt: datetime) -> str:
    """The only timestamp formatter in the project (spec §2).

    One fixed ISO 8601 shape for the DB and raw records, e.g.
    2026-01-05T06:00:00+00:00 (UTC, whole seconds, explicit offset), so TEXT
    timestamps sort chronologically. Never format timestamps anywhere else.
    """
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    """Parse a timestamp in the one fixed format. Anything else raises ValueError."""
    if not isinstance(text, str):
        raise ValueError(f"timestamp must be a string, got {type(text).__name__}")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or to_iso(dt) != text:
        raise ValueError(f"timestamp {text!r} is not in the fixed format, e.g. 2026-01-05T06:00:00+00:00")
    return dt
