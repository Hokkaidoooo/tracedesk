"""Microsoft GraphRAG's pinned Python API in an isolated process per operation.

Process isolation is intentional: GraphRAG 2.7.0 keeps model clients in a global
singleton without a public close contract. Workers own and release those clients.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ..errors import DomainError
from ..evidence import Evidence
from .config import GraphConfig
from .graphrag_settings import official_config
from .projection import build_projection, digest

SUPPORTED_GRAPHRAG_VERSION = "2.7.0"
TABLE_NAMES = (
    "documents",
    "text_units",
    "entities",
    "relationships",
    "communities",
    "community_reports",
)


def require_official_package() -> None:
    try:
        installed = version("graphrag")
    except PackageNotFoundError as exc:
        raise DomainError(
            "graphrag_unavailable", "尚未安装 Microsoft GraphRAG 2.7.0。", 503
        ) from exc
    if installed != SUPPORTED_GRAPHRAG_VERSION:
        raise DomainError(
            "graphrag_version_mismatch", "此适配器仅支持 Microsoft GraphRAG 2.7.0。", 503
        )


def workspace_id(tenant_id: str, snapshot_id: str) -> str:
    return digest(tenant_id) + "-" + digest(snapshot_id)


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def checked_index_manifest(
    workspace: Path,
    evidence: tuple[Evidence, ...],
    artifact: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    """The parent and query worker enforce the same persisted index identity."""
    if fingerprint != artifact.get("settings_digest"):
        raise DomainError("graphrag_config_changed", "GraphRAG 模型配置已改变，请构建新快照。", 409)
    checksums = artifact.get("tables_sha256")
    if not isinstance(checksums, dict) or set(checksums) != set(TABLE_NAMES):
        raise DomainError("graphrag_index_invalid", "GraphRAG 索引缺少完整性清单。", 409)
    for name, expected in checksums.items():
        path = workspace / "output" / f"{name}.parquet"
        if not path.is_file() or file_sha256(path) != expected:
            raise DomainError("graphrag_index_invalid", "GraphRAG 索引表已缺失或改变。", 409)
    manifest_path = workspace / "source-map.json"
    if not manifest_path.is_file() or file_sha256(manifest_path) != artifact.get(
        "source_map_sha256"
    ):
        raise DomainError("graphrag_index_invalid", "GraphRAG 来源映射已缺失或改变。", 409)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    projection = build_projection(evidence, (), GraphConfig())
    expected_scope = {**projection.scope(), "source_digest": projection.source_digest, "schema": 1}
    if any(manifest.get(key) != value for key, value in expected_scope.items()):
        raise DomainError("graphrag_scope_invalid", "GraphRAG 来源映射与目标快照不一致。", 403)
    return manifest


class MicrosoftGraphRag:
    def __init__(self, config: GraphConfig) -> None:
        self.config = config
        self._processes: set[asyncio.subprocess.Process] = set()
        self._closed = False

    async def prepare(self, evidence: tuple[Evidence, ...]) -> dict[str, Any]:
        projection = build_projection(evidence, (), self.config)
        self._configured()
        workspace = self._workspace(projection.tenant_id, projection.snapshot_id)
        try:
            workspace.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise DomainError(
                "graphrag_workspace_exists", "该快照的 GraphRAG 目录已存在；请构建新版本。", 409
            ) from exc
        result = await self._run("build", workspace, evidence, {})
        artifact = result.get("artifact")
        if not isinstance(artifact, dict):
            raise DomainError("graphrag_worker_invalid", "GraphRAG 构建结果缺少索引元数据。", 502)
        self._check_artifact(artifact, evidence)
        return artifact

    async def search(
        self,
        query: str,
        evidence: tuple[Evidence, ...],
        artifact: dict[str, Any],
        *,
        method: str,
        limit: int,
    ) -> tuple[Evidence, ...]:
        if method not in {"graphrag_local", "graphrag_global"}:
            raise DomainError("graph_method_invalid", "Microsoft GraphRAG 仅支持 Local 或 Global。")
        workspace = self.require_ready(evidence, artifact)
        response = await self._run(
            "query",
            workspace,
            evidence,
            {"query": query, "method": method, "limit": limit, "artifact": artifact},
        )
        rows = response.get("evidence")
        if not isinstance(rows, list) or len(rows) > limit:
            raise DomainError("graphrag_worker_invalid", "GraphRAG 检索结果格式或数量无效。", 502)
        known = {item.source_id: item for item in evidence}
        result: list[Evidence] = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"source_id", "score", "graph_path"}
                or row["source_id"] not in known
                or not isinstance(row["graph_path"], list)
                or any(not isinstance(part, str) for part in row["graph_path"])
            ):
                raise DomainError(
                    "graphrag_worker_invalid", "GraphRAG 返回了无法核验的来源引用。", 502
                )
            result.append(
                known[row["source_id"]].ranked(row["score"], method, tuple(row["graph_path"]))
            )
        if len({item.source_id for item in result}) != len(result):
            raise DomainError("graphrag_worker_invalid", "GraphRAG 返回重复来源。", 502)
        return tuple(result)

    def require_ready(
        self,
        evidence: tuple[Evidence, ...],
        artifact: dict[str, Any],
    ) -> Path:
        self._configured()
        self._check_artifact(artifact, evidence)
        workspace = self._workspace(artifact["tenant_id"], artifact["snapshot_id"])
        if not workspace.is_dir():
            raise DomainError(
                "graphrag_index_missing", "该快照的 Microsoft GraphRAG 索引目录不存在。", 409
            )
        _, fingerprint = official_config(Path(self.config.graphrag_settings_path), workspace)
        checked_index_manifest(workspace, evidence, artifact, fingerprint)
        return workspace

    def _configured(self) -> None:
        if self._closed:
            raise DomainError("graphrag_closed", "GraphRAG 服务已关闭。", 503)
        if not self.config.graphrag_enabled or self.config.graphrag_settings_path is None:
            raise DomainError(
                "graphrag_unavailable", "Microsoft GraphRAG 需要显式启用并配置模型设置文件。", 503
            )
        if not Path(self.config.graphrag_settings_path).is_file():
            raise DomainError("graphrag_config_invalid", "Microsoft GraphRAG 设置文件不存在。", 503)
        require_official_package()

    def _workspace(self, tenant_id: str, snapshot_id: str) -> Path:
        root = Path(self.config.graphrag_root).resolve()
        path = root / digest(tenant_id) / digest(snapshot_id)
        if not path.resolve().is_relative_to(root):
            raise DomainError("graphrag_scope_invalid", "GraphRAG 工作目录超出配置范围。", 403)
        return path

    def _check_artifact(self, artifact: dict[str, Any], evidence: tuple[Evidence, ...]) -> None:
        projection = build_projection(evidence, (), self.config)
        expected = {
            "schema": 1,
            "package": "graphrag",
            "version": SUPPORTED_GRAPHRAG_VERSION,
            **projection.scope(),
            "source_digest": projection.source_digest,
            "state": "ready",
            "workspace_id": workspace_id(projection.tenant_id, projection.snapshot_id),
        }
        if any(artifact.get(key) != value for key, value in expected.items()):
            raise DomainError(
                "graphrag_scope_invalid", "Microsoft GraphRAG 索引与固定快照不一致。", 403
            )

    async def _run(
        self,
        operation: str,
        workspace: Path,
        evidence: tuple[Evidence, ...],
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        request = {
            "operation": operation,
            "workspace": str(workspace),
            "settings_path": str(Path(self.config.graphrag_settings_path).resolve()),
            "community_level": self.config.graphrag_community_level,
            "evidence": [item.as_dict() for item in evidence],
            **extra,
        }
        env = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PYTHONUTF8": "1",
        }
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with (workspace / "worker.log").open("ab") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tracedesk.graphs.graphrag_worker",
                cwd=workspace,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                creationflags=flags,
            )
            self._processes.add(process)
            try:
                payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
                stdout, _ = await asyncio.wait_for(
                    process.communicate(payload), self.config.graphrag_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise DomainError(
                    "graphrag_timeout", "Microsoft GraphRAG 执行超过时间预算。", 504
                ) from exc
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
                self._processes.discard(process)
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError(
                "graphrag_worker_invalid",
                "GraphRAG 子进程没有返回有效 JSON；详细错误见快照工作日志。",
                502,
            ) from exc
        if not isinstance(result, dict):
            raise DomainError("graphrag_worker_invalid", "GraphRAG 子进程返回格式无效。", 502)
        if process.returncode != 0 or result.get("ok") is not True:
            raise DomainError(
                result.get("code", "graphrag_runtime_failed"),
                result.get("message", "Microsoft GraphRAG 执行失败；详细错误见快照工作日志。"),
                502,
            )
        return result

    async def close(self) -> None:
        self._closed = True
        processes = tuple(self._processes)
        for process in processes:
            if process.returncode is None:
                process.terminate()
        if processes:
            await asyncio.gather(*(process.wait() for process in processes))
