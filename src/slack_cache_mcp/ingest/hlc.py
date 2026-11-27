"""Hybrid logical clock: <wall_ms>-<counter>-<device_id>."""

from __future__ import annotations

import time
from dataclasses import dataclass


def parse_hlc(value: str) -> tuple[int, int, str]:
    """Parse ``wall-counter-device``; device_id may itself contain hyphens (ULID)."""
    parts = value.split("-", 2)
    if len(parts) != 3:
        raise ValueError(f"invalid HLC: {value!r}")
    return int(parts[0]), int(parts[1]), parts[2]


@dataclass
class HLC:
    device_id: str
    last_wall_ms: int = 0
    counter: int = 0

    def now(self) -> str:
        wall = int(time.time() * 1000)
        if wall > self.last_wall_ms:
            self.last_wall_ms = wall
            self.counter = 0
        else:
            self.counter += 1
            self.last_wall_ms = max(self.last_wall_ms, wall)
        return f"{self.last_wall_ms}-{self.counter}-{self.device_id}"

    def observe(self, remote: str) -> None:
        """Advance past a remote HLC so subsequent ``now()`` values dominate it."""
        try:
            r_wall, r_counter, _device = parse_hlc(remote)
        except ValueError:
            return
        pt = int(time.time() * 1000)
        l_wall, l_counter = self.last_wall_ms, self.counter
        new_wall = max(l_wall, r_wall, pt)
        if new_wall == l_wall == r_wall:
            new_counter = max(l_counter, r_counter) + 1
        elif new_wall == l_wall:
            new_counter = l_counter + 1
        elif new_wall == r_wall:
            new_counter = r_counter + 1
        else:
            new_counter = 0
        self.last_wall_ms = new_wall
        self.counter = new_counter
