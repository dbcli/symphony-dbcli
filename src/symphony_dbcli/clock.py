from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_after(seconds: int) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace(
            "+00:00",
            "Z",
        )
    )


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def monotonic_ns() -> int:
    return time.monotonic_ns()


@dataclass(frozen=True)
class Timestamp:
    wall: str
    monotonic_ns: int


def timestamp() -> Timestamp:
    return Timestamp(wall=utc_now(), monotonic_ns=monotonic_ns())


def elapsed_ms(start_ns: int, end_ns: int | None = None) -> int:
    finish = monotonic_ns() if end_ns is None else end_ns
    return max(0, round((finish - start_ns) / 1_000_000))
