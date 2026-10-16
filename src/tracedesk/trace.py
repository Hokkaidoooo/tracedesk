"""Observable steps rather than private model reasoning."""

from time import perf_counter
from typing import Any


class Trace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    @staticmethod
    def start() -> float:
        return perf_counter()

    def add(self, stage: str, label: str, status: str, detail: str, started: float) -> None:
        self.events.append(
            {
                "seq": len(self.events) + 1,
                "stage": stage,
                "label": label,
                "status": status,
                "detail": detail,
                "duration_ms": round((perf_counter() - started) * 1000, 3),
            }
        )
