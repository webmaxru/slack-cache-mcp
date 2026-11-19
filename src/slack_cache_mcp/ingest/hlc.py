"""Hybrid logical clock: <wall_ms>-<counter>-<device_id>."""

from __future__ import annotations

import time
from dataclasses import dataclass


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
