import asyncio
import json
from importlib.metadata import PackageNotFoundError

import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence
from tracedesk.graphs import GraphConfig
from tracedesk.graphs.graphrag_context import context_evidence, source_map
from tracedesk.graphs.graphrag_settings import settings_values
from tracedesk.graphs.microsoft import MicrosoftGraphRag, require_official_package


def evidence():
    return (
        Evidence(
            "stable-a",
            "tenant-a",
            "snapshot-a",
            "doc-a",
            1,
            "资料一",
            "Alpha 使用 Beta。",
            "a" * 64,
        ),
        Evidence(
            "stable-b",
            "tenant-a",
            "snapshot-a",
            "doc-b",
            2,
            "资料二",
            "Beta 遵循 Gamma。",
            "b" * 64,
            page=3,
        ),
    )


def tables():
    return {
        "documents": [{"id": item.source_id, "text": item.text} for item in evidence()],
        "text_units": [
            {"id": "unit-a", "human_readable_id": 101, "document_ids": ["stable-a"]},
            {"id": "unit-b", "human_readable_id": 102, "document_ids": ["stable-b"]},
        ],
        "entities": [{"id": "entity-a", "human_readable_id": 8, "text_unit_ids": ["unit-a"]}],
        "relationships": [
            {"id": "edge-a", "human_readable_id": 9, "text_unit_ids": ["unit-a", "unit-b"]}
        ],
        "communities": [{"community": 7, "text_unit_ids": ["unit-a", "unit-b"]}],
        "community_reports": [{"id": "report-a", "human_readable_id": 500, "community": 7}],
    }


def test_local_source_blocks_and_global_communities_map_to_original_evidence():
    source = evidence()
    mapping = source_map(tables(), source)
    local = context_evidence(
        {"sources": [{"id": "102"}]}, mapping, source, method="graphrag_local", limit=5
    )
    assert local[0].source_id == "stable-b" and local[0].text == source[1].text
    assert local[0].document_version == 2 and local[0].page == 3
    global_result = context_evidence(
        {"reports": [{"id": "7"}]}, mapping, source, method="graphrag_global", limit=5
    )
    assert [item.source_id for item in global_result] == ["stable-a", "stable-b"]
    with pytest.raises(DomainError):
        context_evidence(
            {"reports": [{"id": "500"}]}, mapping, source, method="graphrag_global", limit=5
        )


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("documents", "id", "other-tenant-source"),
        ("documents", "text", "changed original"),
        ("text_units", "document_ids", ["other-snapshot-source"]),
        ("entities", "text_unit_ids", ["unknown-unit"]),
        ("communities", "text_unit_ids", []),
        ("community_reports", "community", 999),
    ],
)
def test_index_maps_cannot_invent_or_cross_source_ids(table, field, value):
    data = tables()
    data[table][0][field] = value
    with pytest.raises(DomainError) as error:
        source_map(data, evidence())
    assert error.value.code == "graphrag_provenance_invalid"


def test_context_candidates_are_excluded_and_unknown_selected_ids_fail():
    mapping = source_map(tables(), evidence())
    result = context_evidence(
        {"sources": [{"id": "unknown", "in_context": False}, {"id": "101", "in_context": True}]},
        mapping,
        evidence(),
        method="graphrag_local",
        limit=5,
    )
    assert [item.source_id for item in result] == ["stable-a"]
    for context in (
        {"sources": [{"id": "unknown"}]},
        {"unmapped": [{"id": "101"}]},
        {"sources": [{"id": "101", "in_context": "false"}]},
    ):
        with pytest.raises(DomainError):
            context_evidence(context, mapping, evidence(), method="graphrag_local", limit=5)


def settings_file(tmp_path, **overrides):
    path = tmp_path / "settings.json"
    settings = {
        "models": {
            "default_chat_model": {"type": "openai_chat", "model": "configured-chat"},
            "default_embedding_model": {
                "type": "openai_embedding",
                "model": "configured-embedding",
            },
        },
        **overrides,
    }
    path.write_text(json.dumps(settings), encoding="utf-8")
    return path


def test_all_official_storage_is_bound_to_new_snapshot_directory(tmp_path):
    path = settings_file(
        tmp_path,
        output={"type": "file", "base_dir": "outside"},
        chunks={"size": 100, "group_by_columns": ["title"]},
    )
    workspace = tmp_path / "snapshot"
    values, fingerprint = settings_values(path, workspace)
    for field in ("output", "cache", "reporting", "update_index_output"):
        assert values[field]["base_dir"].startswith(str(workspace))
    assert values["input"]["storage"]["base_dir"].startswith(str(workspace))
    assert values["vector_store"]["default_vector_store"]["db_uri"].startswith(str(workspace))
    assert values["chunks"]["group_by_columns"] == ["id"]
    assert len(fingerprint) == 64


def test_missing_env_and_cloud_vector_store_are_explicit_configuration_errors(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TRACEDESK_TEST_MISSING_GRAPHRAG_KEY", raising=False)
    path = settings_file(tmp_path)
    path.write_text(
        '{"models":{"chat":{"model":"${TRACEDESK_TEST_MISSING_GRAPHRAG_KEY}"}}}', encoding="utf-8"
    )
    with pytest.raises(DomainError) as error:
        settings_values(path, tmp_path / "snapshot")
    assert error.value.code == "graphrag_config_invalid"
    path = settings_file(
        tmp_path, vector_store={"default_vector_store": {"type": "azure_ai_search"}}
    )
    with pytest.raises(DomainError):
        settings_values(path, tmp_path / "snapshot")


def test_official_init_prompt_files_resolve_from_settings_directory_and_are_fingerprinted(tmp_path):
    prompt = tmp_path / "extract.txt"
    prompt.write_text("First extraction prompt", encoding="utf-8")
    path = settings_file(tmp_path, extract_graph={"prompt": "extract.txt"})
    values, before = settings_values(path, tmp_path / "snapshot")
    assert values["extract_graph"]["prompt"] == str(prompt.resolve())
    prompt.write_text("Updated extraction prompt", encoding="utf-8")
    _, after = settings_values(path, tmp_path / "snapshot")
    assert before != after


def test_missing_official_package_does_not_build_a_local_graph_substitute(tmp_path, monkeypatch):
    def missing(_):
        raise PackageNotFoundError("graphrag")

    monkeypatch.setattr("tracedesk.graphs.microsoft.version", missing)
    config = GraphConfig(
        graphrag_enabled=True,
        graphrag_settings_path=settings_file(tmp_path),
        graphrag_root=tmp_path / "workspaces",
    )
    with pytest.raises(DomainError) as error:
        asyncio.run(MicrosoftGraphRag(config).prepare(evidence()))
    assert error.value.code == "graphrag_unavailable"
    assert not config.graphrag_root.exists()


def test_other_official_versions_do_not_silently_use_the_pinned_api(monkeypatch):
    monkeypatch.setattr("tracedesk.graphs.microsoft.version", lambda _: "9.0.0")
    with pytest.raises(DomainError) as error:
        require_official_package()
    assert error.value.code == "graphrag_version_mismatch"


def test_workspace_uses_hashed_scope_not_user_supplied_path_segments(tmp_path):
    runtime = MicrosoftGraphRag(GraphConfig(graphrag_root=tmp_path))
    scoped = runtime._workspace("..\\outside", "..\\snapshot")
    assert scoped.parent.parent == tmp_path.resolve()
    assert "outside" not in str(scoped)
    assert runtime._workspace("a", "same") != runtime._workspace("b", "same")
    assert runtime._workspace("a", "one") != runtime._workspace("a", "two")
