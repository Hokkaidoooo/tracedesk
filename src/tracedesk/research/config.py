"""Explicit configuration for bounded, read-only model research."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

RETRIEVAL_METHODS = (
    "lexical",
    "semantic",
    "hybrid",
    "graph",
    "text2cypher",
    "graphrag_local",
    "graphrag_global",
)


@dataclass(frozen=True)
class ResearchConfig:
    mode: str = "multi"
    available_methods: tuple[str, ...] = (
        "lexical",
        "semantic",
        "hybrid",
        "graph",
        "text2cypher",
    )
    max_model_calls: int = 16
    max_tasks: int = 6
    max_parallel: int = 3
    max_context_chars: int = 240_000
    max_retrieval_calls: int = 8
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in {"multi", "single"}:
            raise ValueError("Research mode must be multi or single.")
        if (
            not isinstance(self.available_methods, tuple)
            or not self.available_methods
            or any(method not in RETRIEVAL_METHODS for method in self.available_methods)
            or len(set(self.available_methods)) != len(self.available_methods)
        ):
            raise ValueError("Research requires distinct, explicitly available retrieval methods.")
        if any(method.startswith("graphrag_") for method in self.available_methods):
            raise ValueError(
                "Research cannot budget Microsoft GraphRAG internal model calls yet; "
                "use its separate retrieval endpoint."
            )
        for name in (
            "max_model_calls",
            "max_tasks",
            "max_parallel",
            "max_context_chars",
            "max_retrieval_calls",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"Research {name} must be a positive integer.")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("Research timeout_seconds must be finite and positive.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ResearchConfig":
        env = os.environ if environ is None else environ
        values: dict = {}
        for name in (
            "max_model_calls",
            "max_tasks",
            "max_parallel",
            "max_context_chars",
            "max_retrieval_calls",
        ):
            key = f"TRACEDESK_RESEARCH_{name.upper()}"
            if key in env:
                try:
                    values[name] = int(env[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be a positive integer.") from exc
        if "TRACEDESK_RESEARCH_TIMEOUT_SECONDS" in env:
            try:
                values["timeout_seconds"] = float(env["TRACEDESK_RESEARCH_TIMEOUT_SECONDS"])
            except (TypeError, ValueError) as exc:
                raise ValueError("TRACEDESK_RESEARCH_TIMEOUT_SECONDS must be a number.") from exc
        if "TRACEDESK_RESEARCH_MODE" in env:
            values["mode"] = env["TRACEDESK_RESEARCH_MODE"]
        if "TRACEDESK_RESEARCH_METHODS" in env:
            values["available_methods"] = tuple(
                method.strip() for method in env["TRACEDESK_RESEARCH_METHODS"].split(",")
            )
        return cls(**values)

    def budgets(self) -> dict[str, int | float]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_tasks": self.max_tasks,
            "max_parallel": self.max_parallel,
            "max_context_chars": self.max_context_chars,
            "max_retrieval_calls": self.max_retrieval_calls,
            "timeout_seconds": self.timeout_seconds,
        }
