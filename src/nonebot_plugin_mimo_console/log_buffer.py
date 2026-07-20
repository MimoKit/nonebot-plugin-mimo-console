from __future__ import annotations

import time
from collections import deque
from threading import RLock
from typing import Any


class LogBuffer:
    def __init__(
        self,
        capacity: int = 1000,
        ignored_fragments: tuple[str, ...] = (),
    ) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._sequence = 0
        self._lock = RLock()
        self._ignored_fragments = ignored_fragments

    def sink(self, message: Any) -> None:
        record = message.record
        content = str(record["message"])
        if any(fragment in content for fragment in self._ignored_fragments):
            return
        with self._lock:
            self._sequence += 1
            self._items.append(
                {
                    "id": self._sequence,
                    "time": record["time"].isoformat(),
                    "level": record["level"].name,
                    "message": content,
                    "module": record["name"],
                }
            )

    def list(self, after: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock:
            return [item.copy() for item in self._items if item["id"] > after][-limit:]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


STARTED_AT = time.time()
