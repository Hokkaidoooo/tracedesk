"""Explicit limits and optional graph backend configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GraphConfig:
    backend: str = "local"
    extraction: str = "explicit"
    max_hops: int = 3
    max_facts: int = 5000
    max_nodes: int = 2000
    max_expansions: int = 20000
    max_rows: int = 20
    query_timeout_seconds: float = 10.0
    text2cypher_attempts: int = 2
    max_prompt_chars: int = 24000
    graphrag_enabled: bool = False
    graphrag_root: Path = Path(".tracedesk") / "graphrag"
    graphrag_settings_path: Path | None = None
    graphrag_community_level: int = 2
    graphrag_timeout_seconds: float = 600.0
    neo4j_uri: str | None = None
    neo4j_database: str = "neo4j"
    neo4j_reader_user: str | None = None
    neo4j_reader_password: str | None = field(default=None, repr=False)
    neo4j_writer_user: str | None = None
    neo4j_writer_password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.backend not in {"local", "neo4j"}:
            raise ValueError("Graph backend must be local or neo4j.")
        if self.extraction not in {"explicit", "model"}:
            raise ValueError("Graph extraction must be explicit or model.")
        bounds = {
            "max_hops": (1, 6),
            "max_facts": (1, 100000),
            "max_nodes": (1, 50000),
            "max_expansions": (1, 1000000),
            "max_rows": (1, 100),
            "text2cypher_attempts": (1, 3),
            "max_prompt_chars": (1000, 1000000),
            "graphrag_community_level": (0, 20),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}].")
        if not 0 < self.query_timeout_seconds <= 120:
            raise ValueError("Graph query timeout must be in (0, 120] seconds.")
        if not 0 < self.graphrag_timeout_seconds <= 86400:
            raise ValueError("GraphRAG timeout must be in (0, 86400] seconds.")
        if type(self.graphrag_enabled) is not bool:
            raise ValueError("graphrag_enabled must be boolean.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "GraphConfig":
        env = os.environ if environ is None else environ
        values: dict[str, object] = {}
        for name in (
            "backend",
            "extraction",
            "max_hops",
            "max_facts",
            "max_nodes",
            "max_expansions",
            "max_rows",
            "query_timeout_seconds",
            "text2cypher_attempts",
            "max_prompt_chars",
        ):
            key = f"TRACEDESK_GRAPH_{name.upper()}"
            if key in env:
                values[name] = _number_or_text(name, env[key])
        for name in (
            "uri",
            "database",
            "reader_user",
            "reader_password",
            "writer_user",
            "writer_password",
        ):
            key = f"TRACEDESK_NEO4J_{name.upper()}"
            if key in env:
                values[f"neo4j_{name}"] = env[key]
        for suffix, name in (("ROOT", "root"), ("SETTINGS", "settings_path")):
            if f"TRACEDESK_GRAPHRAG_{suffix}" in env:
                raw = env[f"TRACEDESK_GRAPHRAG_{suffix}"]
                if not raw.strip():
                    raise ValueError(f"TRACEDESK_GRAPHRAG_{suffix} must not be empty.")
                values[f"graphrag_{name}"] = Path(raw)
        enabled = env.get("TRACEDESK_GRAPHRAG_ENABLED")
        if enabled is not None:
            if enabled.lower() not in {"true", "false", "1", "0"}:
                raise ValueError("TRACEDESK_GRAPHRAG_ENABLED must be true, false, 1 or 0.")
            values["graphrag_enabled"] = enabled.lower() in {"true", "1"}
        for suffix, converter in (("COMMUNITY_LEVEL", int), ("TIMEOUT_SECONDS", float)):
            if f"TRACEDESK_GRAPHRAG_{suffix}" in env:
                values[f"graphrag_{suffix.lower()}"] = converter(
                    env[f"TRACEDESK_GRAPHRAG_{suffix}"]
                )
        return cls(**values)


def _number_or_text(name: str, value: str) -> str | int | float:
    if name in {"backend", "extraction"}:
        return value
    return float(value) if name.endswith("seconds") else int(value)
