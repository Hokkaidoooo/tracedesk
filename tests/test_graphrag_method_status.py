"""File/configuration readiness tests; fixture tables are never used as real Parquet indexes."""

import asyncio
import json
import sys
from dataclasses import replace
from types import ModuleType

import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence
from tracedesk.graphs import GraphConfig, GraphService
from tracedesk.graphs.graphrag_settings import settings_values
from tracedesk.graphs.graphrag_worker import _query
from tracedesk.graphs.microsoft import (
    SUPPORTED_GRAPHRAG_VERSION,
    TABLE_NAMES,
    MicrosoftGraphRag,
    file_sha256,
    workspace_id,
)
from tracedesk.graphs.projection import build_projection

SOURCE = Evidence(
    "source", "tenant", "snapshot", "document", 1, "说明", "Alpha 使用 Beta。", "a" * 64
)


def index_fixture(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"models": {"chat": {"model": "fixture-model"}}}), encoding="utf-8"
    )
    config = GraphConfig(
        graphrag_enabled=True, graphrag_settings_path=settings, graphrag_root=tmp_path / "indexes"
    )
    runtime = MicrosoftGraphRag(config)
    workspace = runtime._workspace(SOURCE.tenant_id, SOURCE.snapshot_id)
    output = workspace / "output"
    output.mkdir(parents=True)
    for name in TABLE_NAMES:
        (output / f"{name}.parquet").write_bytes(f"readiness-only-fixture:{name}".encode())
    projection = build_projection((SOURCE,), (), config)
    manifest = {
        "schema": 1,
        **projection.scope(),
        "source_digest": projection.source_digest,
        "mapping": {},
    }
    manifest_path = workspace / "source-map.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _, fingerprint = settings_values(settings, workspace)
    artifact = {
        "schema": 1,
        "package": "graphrag",
        "version": SUPPORTED_GRAPHRAG_VERSION,
        **projection.scope(),
        "source_digest": projection.source_digest,
        "state": "ready",
        "workspace_id": workspace_id(SOURCE.tenant_id, SOURCE.snapshot_id),
        "settings_digest": fingerprint,
        "source_map_sha256": file_sha256(manifest_path),
        "tables_sha256": {name: file_sha256(output / f"{name}.parquet") for name in TABLE_NAMES},
    }
    outer = projection.artifact(extraction="explicit", backend="local")
    outer["graphrag"] = artifact
    monkeypatch.setattr("tracedesk.graphs.microsoft.require_official_package", lambda: None)
    monkeypatch.setattr("tracedesk.graphs.microsoft.official_config", settings_values)
    monkeypatch.setattr("tracedesk.graphs.graphrag_worker.official_config", settings_values)

    async def forbidden_worker(*args, **kwargs):
        raise AssertionError("Readiness checks must not start a GraphRAG worker.")

    monkeypatch.setattr(runtime, "_run", forbidden_worker)
    return config, runtime, workspace, {"graph": outer}


def forbid_official_queries(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("An invalid index must not reach an official model query.")

    module = ModuleType("graphrag.api.query")
    module.local_search = forbidden
    module.global_search = forbidden
    monkeypatch.setitem(sys.modules, "graphrag.api.query", module)


def test_graphrag_status_checks_the_local_index_without_starting_a_worker(tmp_path, monkeypatch):
    config, runtime, workspace, saved = index_fixture(tmp_path, monkeypatch)
    service = GraphService(config, graphrag=runtime)
    statuses = service.method_status((SOURCE,), saved)
    assert statuses["graph"]["error"]["code"] == "graph_empty"
    assert statuses["graphrag_local"] == {"available": True, "error": None}
    assert statuses["graphrag_global"] == {"available": True, "error": None}
    assert runtime.require_ready((SOURCE,), saved["graph"]["graphrag"]) == workspace


@pytest.mark.parametrize(
    "case,code",
    [
        ("model_changed", "graphrag_config_changed"),
        ("table_changed", "graphrag_index_invalid"),
        ("incomplete_manifest", "graphrag_index_invalid"),
        ("source_map_changed", "graphrag_index_invalid"),
        ("foreign_scope", "graphrag_scope_invalid"),
    ],
)
def test_graphrag_status_and_worker_reject_the_same_index_failure(
    tmp_path, monkeypatch, case, code
):
    config, runtime, workspace, saved = index_fixture(tmp_path, monkeypatch)
    artifact = saved["graph"]["graphrag"]
    if case == "model_changed":
        config.graphrag_settings_path.write_text(
            json.dumps({"models": {"chat": {"model": "changed-model"}}}),
            encoding="utf-8",
        )
    elif case == "table_changed":
        (workspace / "output" / "entities.parquet").write_bytes(b"changed fixture table")
    elif case == "incomplete_manifest":
        artifact["tables_sha256"].pop("entities")
    elif case == "source_map_changed":
        (workspace / "source-map.json").write_text("{}", encoding="utf-8")
    else:
        path = workspace / "source-map.json"
        values = json.loads(path.read_text(encoding="utf-8"))
        values["tenant_id"] = "different-tenant"
        path.write_text(json.dumps(values), encoding="utf-8")
        artifact["source_map_sha256"] = file_sha256(path)

    service = GraphService(config, graphrag=runtime)
    statuses = service.method_status((SOURCE,), saved)
    for method in ("graphrag_local", "graphrag_global"):
        with pytest.raises(DomainError) as failure:
            asyncio.run(service.search("Alpha", (SOURCE,), saved, method=method))
        assert failure.value.code == code
        assert statuses[method] == {"available": False, "error": failure.value.as_dict()}

    forbid_official_queries(monkeypatch)
    with pytest.raises(DomainError) as worker_failure:
        asyncio.run(
            _query(
                {
                    "workspace": str(workspace),
                    "artifact": artifact,
                    "settings_path": str(config.graphrag_settings_path),
                },
                (SOURCE,),
            )
        )
    assert worker_failure.value.as_dict() == statuses["graphrag_local"]["error"]


def test_missing_graphrag_directory_and_settings_are_visible(tmp_path, monkeypatch):
    config, _, _, saved = index_fixture(tmp_path, monkeypatch)
    for changed, code in (
        (replace(config, graphrag_root=tmp_path / "missing-indexes"), "graphrag_index_missing"),
        (
            replace(config, graphrag_settings_path=tmp_path / "missing.json"),
            "graphrag_config_invalid",
        ),
    ):
        service = GraphService(changed)
        statuses = service.method_status((SOURCE,), saved)
        assert statuses["graphrag_local"]["error"]["code"] == code
        with pytest.raises(DomainError) as failure:
            asyncio.run(service.search("Alpha", (SOURCE,), saved, method="graphrag_local"))
        assert failure.value.as_dict() == statuses["graphrag_local"]["error"]
        asyncio.run(service.close())
