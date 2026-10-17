"""Configuration is explicit; selecting a model never enables an offline fallback."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .graphs import GraphConfig
from .knowledge import KnowledgeConfig
from .research import ResearchConfig


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path(".tracedesk/tracedesk.db")
    provider: str = "extractive"
    retrieval_method: str = "ngram"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = field(default="", repr=False)
    request_timeout_seconds: float = 30.0
    max_model_steps: int = 6
    max_tool_calls: int = 12
    max_message_chars: int = 2000
    max_history_turns: int = 6
    max_completion_tokens: int = 1800
    max_tool_output_chars: int = 16000
    action_ttl_seconds: int = 600
    run_lease_seconds: int = 300
    demo_mode: bool = True
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    graphs: GraphConfig = field(default_factory=GraphConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)

    def __post_init__(self) -> None:
        if self.provider not in {"extractive", "llm"}:
            raise ValueError("TRACEDESK_PROVIDER 必须为 extractive 或 llm。")
        if self.retrieval_method not in {"bm25", "ngram", "hybrid"}:
            raise ValueError("TRACEDESK_RETRIEVAL_METHOD 必须为 bm25、ngram 或 hybrid。")
        positive = (
            self.request_timeout_seconds,
            self.max_model_steps,
            self.max_tool_calls,
            self.max_message_chars,
            self.max_history_turns,
            self.max_completion_tokens,
            self.max_tool_output_chars,
            self.action_ttl_seconds,
            self.run_lease_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError("预算、长度和有效期配置必须为正数。")
        if self.run_lease_seconds <= self.request_timeout_seconds * self.max_model_steps:
            raise ValueError("运行租约必须长于单步模型超时乘以最大模型步数。")
        if not self.demo_mode:
            raise ValueError("此版本只提供演示身份；真实身份适配尚未实现，不能关闭演示声明。")
        if self.provider == "extractive":
            return
        parsed = urlparse(self.base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("模型地址必须为 HTTPS；仅本机模型允许 HTTP。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("模型地址不能包含凭据、查询参数或片段。")
        if not self.model.strip():
            raise ValueError("模型模式必须指定 TRACEDESK_MODEL。")
        if not local and not self.api_key.strip():
            raise ValueError("远程模型模式必须提供 TRACEDESK_API_KEY。")

    @classmethod
    def from_env(cls) -> "Settings":
        def integer(name: str, default: int) -> int:
            raw = os.environ.get(f"TRACEDESK_{name}", str(default))
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"TRACEDESK_{name} 必须为整数。") from exc

        demo = os.environ.get("TRACEDESK_DEMO_MODE", "true").lower()
        if demo not in {"true", "false"}:
            raise ValueError("TRACEDESK_DEMO_MODE 必须为 true 或 false。")
        return cls(
            database_path=Path(os.environ.get("TRACEDESK_DATABASE", ".tracedesk/tracedesk.db")),
            provider=os.environ.get("TRACEDESK_PROVIDER", "extractive"),
            retrieval_method=os.environ.get("TRACEDESK_RETRIEVAL_METHOD", "ngram"),
            base_url=os.environ.get("TRACEDESK_BASE_URL", "https://api.deepseek.com/v1"),
            model=os.environ.get("TRACEDESK_MODEL", "deepseek-chat"),
            api_key=os.environ.get("TRACEDESK_API_KEY", ""),
            request_timeout_seconds=float(os.environ.get("TRACEDESK_TIMEOUT_SECONDS", "30")),
            max_model_steps=integer("MAX_MODEL_STEPS", 6),
            max_tool_calls=integer("MAX_TOOL_CALLS", 12),
            max_message_chars=integer("MAX_MESSAGE_CHARS", 2000),
            max_history_turns=integer("MAX_HISTORY_TURNS", 6),
            max_completion_tokens=integer("MAX_COMPLETION_TOKENS", 1800),
            max_tool_output_chars=integer("MAX_TOOL_OUTPUT_CHARS", 16000),
            action_ttl_seconds=integer("ACTION_TTL_SECONDS", 600),
            run_lease_seconds=integer("RUN_LEASE_SECONDS", 300),
            demo_mode=demo == "true",
            knowledge=KnowledgeConfig.from_env(),
            graphs=GraphConfig.from_env(),
            research=ResearchConfig.from_env(),
        )
