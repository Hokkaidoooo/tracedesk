"""Load explicit model settings while fixing every GraphRAG storage scope."""

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from string import Template
from typing import Any

from ..errors import DomainError
from .projection import digest


def settings_values(path: Path, workspace: Path) -> tuple[dict[str, Any], str]:
    try:
        expanded = Template(path.read_text(encoding="utf-8")).substitute(os.environ)
    except (KeyError, ValueError) as exc:
        raise DomainError(
            "graphrag_config_invalid", "GraphRAG 模型配置引用了未设置的环境变量。", 503
        ) from exc
    if path.suffix.lower() == ".json":
        try:
            values = json.loads(expanded)
        except json.JSONDecodeError as exc:
            raise DomainError(
                "graphrag_config_invalid", "GraphRAG JSON 配置格式无效。", 503
            ) from exc
    elif path.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        try:
            values = yaml.safe_load(expanded)
        except yaml.YAMLError as exc:
            raise DomainError(
                "graphrag_config_invalid", "GraphRAG YAML 配置格式无效。", 503
            ) from exc
    else:
        raise DomainError("graphrag_config_invalid", "GraphRAG 配置必须是 JSON 或 YAML 文件。", 503)
    if (
        not isinstance(values, dict)
        or not isinstance(values.get("models"), dict)
        or not values["models"]
    ):
        raise DomainError("graphrag_config_invalid", "GraphRAG 必须显式配置聊天和嵌入模型。", 503)
    if any(
        not isinstance(model, dict) or not model.get("model") for model in values["models"].values()
    ):
        raise DomainError("graphrag_config_invalid", "GraphRAG 模型设置缺少模型名称。", 503)
    scoped = deepcopy(values)
    prompt_hashes = _resolve_prompts(scoped, path.parent)
    settings_digest = digest({"settings": values, "prompt_hashes": prompt_hashes})
    scoped["input"] = {
        "file_type": "text",
        "storage": {"type": "file", "base_dir": str(workspace / "input")},
    }
    for field, directory in (
        ("output", "output"),
        ("cache", "cache"),
        ("reporting", "logs"),
        ("update_index_output", "update_output"),
    ):
        scoped[field] = {"type": "file", "base_dir": str(workspace / directory)}
    scoped["outputs"] = None
    scoped["chunks"] = {**scoped.get("chunks", {}), "group_by_columns": ["id"]}
    if scoped.get("extract_claims", {}).get("enabled", False):
        raise DomainError(
            "graphrag_config_invalid",
            "此适配器未提供 claims 来源映射，请关闭 extract_claims。",
            503,
        )
    stores = scoped.get("vector_store", {"default_vector_store": {}})
    if not isinstance(stores, dict) or not stores:
        raise DomainError("graphrag_config_invalid", "GraphRAG vector_store 配置无效。", 503)
    for name, store in stores.items():
        if not isinstance(store, dict) or store.get("type", "lancedb") != "lancedb":
            raise DomainError(
                "graphrag_config_invalid", "此适配器要求每个快照独立的本地 LanceDB。", 503
            )
        store["type"] = "lancedb"
        store["db_uri"] = str(workspace / "vectors" / digest(name))
    scoped["vector_store"] = stores
    return scoped, settings_digest


def _resolve_prompts(values: dict[str, Any], settings_directory: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    sections = (
        "extract_graph",
        "summarize_descriptions",
        "community_reports",
        "extract_claims",
        "local_search",
        "global_search",
        "basic_search",
        "drift_search",
    )
    for section in sections:
        settings = values.get(section, {})
        if not isinstance(settings, dict):
            raise DomainError("graphrag_config_invalid", "GraphRAG 算法配置必须是对象。", 503)
        for key, value in settings.items():
            if not (key == "prompt" or key.endswith("_prompt")) or value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise DomainError("graphrag_config_invalid", "GraphRAG 提示词文件路径无效。", 503)
            prompt = (settings_directory / value).resolve()
            if not prompt.is_file():
                raise DomainError(
                    "graphrag_config_invalid", "GraphRAG 配置引用的提示词文件不存在。", 503
                )
            hashes[f"{section}.{key}"] = hashlib.sha256(prompt.read_bytes()).hexdigest()
            settings[key] = str(prompt)
    return hashes


def official_config(path: Path, workspace: Path) -> tuple[Any, str]:
    from graphrag.config.create_graphrag_config import create_graphrag_config

    values, fingerprint = settings_values(path, workspace)
    return create_graphrag_config(values, root_dir=str(workspace)), fingerprint
