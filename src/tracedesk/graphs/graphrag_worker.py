"""Internal process entry point for Microsoft's official GraphRAG 2.7.0 API."""

import asyncio
import json
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from ..errors import DomainError
from ..evidence import Evidence
from .config import GraphConfig
from .graphrag_context import context_evidence, source_map
from .graphrag_settings import official_config
from .microsoft import (
    SUPPORTED_GRAPHRAG_VERSION,
    TABLE_NAMES,
    checked_index_manifest,
    file_sha256,
    require_official_package,
    workspace_id,
)
from .projection import build_projection

MAX_REQUEST_BYTES = 64 * 1024 * 1024


def _tables(workspace: Path) -> dict[str, Any]:
    import pandas as pd

    result = {}
    for name in TABLE_NAMES:
        path = workspace / "output" / f"{name}.parquet"
        if not path.is_file():
            raise DomainError("graphrag_index_missing", f"GraphRAG 构建缺少 {name} 输出表。", 409)
        result[name] = pd.read_parquet(path)
    return result


def _evidence(records: list[dict[str, Any]]) -> tuple[Evidence, ...]:
    result = []
    for record in records:
        values = {key: value for key, value in record.items() if key != "quote"}
        values["graph_path"] = tuple(values["graph_path"])
        result.append(Evidence(**values))
    return tuple(result)


async def _build(request: dict[str, Any], evidence: tuple[Evidence, ...]) -> dict[str, Any]:
    import pandas as pd
    from graphrag.api.index import build_index

    workspace = Path(request["workspace"]).resolve()
    config, fingerprint = official_config(Path(request["settings_path"]), workspace)
    documents = pd.DataFrame(
        [
            {
                "id": item.source_id,
                "title": item.title,
                "text": item.text,
                "creation_date": "",
                "metadata": {"source_id": item.source_id},
            }
            for item in evidence
        ]
    )
    outputs = await build_index(
        config=config, method="standard", is_update_run=False, input_documents=documents
    )
    if not outputs or any(output.errors for output in outputs):
        failed = [output.workflow for output in outputs if output.errors]
        raise DomainError(
            "graphrag_build_failed",
            "Microsoft GraphRAG 索引构建失败；没有发布该快照。",
            502,
            {"failed_workflows": failed},
        )
    tables = _tables(workspace)
    mapping = source_map(
        {name: table.to_dict(orient="records") for name, table in tables.items()}, evidence
    )
    projection = build_projection(evidence, (), GraphConfig())
    manifest = {
        "schema": 1,
        **projection.scope(),
        "source_digest": projection.source_digest,
        "mapping": mapping,
    }
    manifest_path = workspace / "source-map.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    artifact = {
        "schema": 1,
        "package": "graphrag",
        "version": SUPPORTED_GRAPHRAG_VERSION,
        **projection.scope(),
        "source_digest": projection.source_digest,
        "state": "ready",
        "workspace_id": workspace_id(projection.tenant_id, projection.snapshot_id),
        "settings_digest": fingerprint,
        "source_map_sha256": file_sha256(manifest_path),
        "tables_sha256": {
            name: file_sha256(workspace / "output" / f"{name}.parquet") for name in TABLE_NAMES
        },
    }
    return {"ok": True, "artifact": artifact}


async def _query(request: dict[str, Any], evidence: tuple[Evidence, ...]) -> dict[str, Any]:
    from graphrag.api.query import global_search, local_search

    workspace = Path(request["workspace"]).resolve()
    artifact = request["artifact"]
    config, fingerprint = official_config(Path(request["settings_path"]), workspace)
    manifest = checked_index_manifest(workspace, evidence, artifact, fingerprint)
    tables = _tables(workspace)
    kwargs = {
        "config": config,
        "entities": tables["entities"],
        "communities": tables["communities"],
        "community_reports": tables["community_reports"],
        "community_level": request["community_level"],
        "response_type": "Multiple Paragraphs",
        "query": request["query"],
    }
    if request["method"] == "graphrag_local":
        _, context = await local_search(
            **kwargs,
            text_units=tables["text_units"],
            relationships=tables["relationships"],
            covariates=None,
        )
    elif request["method"] == "graphrag_global":
        _, context = await global_search(**kwargs, dynamic_community_selection=False)
    else:
        raise DomainError("graph_method_invalid", "未知的 Microsoft GraphRAG 检索方式。")
    selected = context_evidence(
        context, manifest["mapping"], evidence, method=request["method"], limit=request["limit"]
    )
    return {
        "ok": True,
        "evidence": [
            {"source_id": item.source_id, "score": item.score, "graph_path": list(item.graph_path)}
            for item in selected
        ],
    }


async def execute_request(request: dict[str, Any]) -> dict[str, Any]:
    require_official_package()
    evidence = _evidence(request["evidence"])
    build_projection(evidence, (), GraphConfig())
    if request["operation"] == "build":
        return await _build(request, evidence)
    if request["operation"] == "query":
        return await _query(request, evidence)
    raise DomainError("graphrag_worker_invalid", "未知的 GraphRAG 子进程操作。")


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise DomainError("graphrag_input_budget", "GraphRAG 子进程输入超过 64 MiB 预算。", 422)
        request = json.loads(raw)
        with redirect_stdout(sys.stderr):
            result = asyncio.run(execute_request(request))
    except DomainError as exc:
        traceback.print_exc(file=sys.stderr)
        result = {"ok": False, "code": exc.code, "message": exc.message}
    except Exception as exc:
        # At the process boundary failures are reported and logged, never replaced with results.
        traceback.print_exc(file=sys.stderr)
        result = {
            "ok": False,
            "code": "graphrag_runtime_failed",
            "message": f"Microsoft GraphRAG 执行失败（{type(exc).__name__}）；详细错误见快照工作日志。",
        }
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
