import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tracedesk import cli
from tracedesk.bootstrap import Services


def run_cli(tmp_path, *arguments, extra_env=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TRACEDESK_")}
    env.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONIOENCODING": "cp1252",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-B", "-m", "tracedesk.cli", *map(str, arguments)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        timeout=60,
    )


def json_result(result):
    assert result.returncode == 0, result.stderr.decode("utf-8")
    return json.loads(result.stdout.decode("utf-8"))


def test_demo_list_and_search_use_published_database_from_both_option_positions(tmp_path):
    database = tmp_path / "demo.db"
    loaded = json_result(run_cli(tmp_path, "knowledge", "--database", database, "load-demo"))
    assert len(loaded["documents"]) == 4
    assert loaded["methods"] == ["lexical", "graph"]
    listing = json_result(
        run_cli(
            tmp_path,
            "knowledge",
            "list",
            "--database",
            database,
            "--customer",
            "customer-b",
            extra_env={"TRACEDESK_API_KEY": "private-test-key"},
        )
    )
    assert listing["current_snapshot"]["id"] == loaded["loaded_snapshot_id"]
    assert "private-test-key" not in json.dumps(listing)
    result = json_result(
        run_cli(
            tmp_path,
            "knowledge",
            "search",
            "X100",
            "--database",
            database,
            "--method",
            "graph",
            "--snapshot-id",
            loaded["loaded_snapshot_id"],
        )
    )
    assert result["evidence"] and result["snapshot_id"] == loaded["loaded_snapshot_id"]
    assert all(item["snapshot_id"] == result["snapshot_id"] for item in result["evidence"])
    assert "UnicodeEncodeError" not in json.dumps(result)


def test_ingest_build_relations_and_historical_snapshot_are_persisted(tmp_path):
    database = tmp_path / "documents.db"
    document = tmp_path / "notes.md"
    document.write_text("# 设备记录\n\nAlpha 使用 Beta。\n", encoding="utf-8")
    ingested = json_result(
        run_cli(
            tmp_path, "knowledge", "ingest", document, "--title", "设备记录", "--database", database
        )
    )
    assert ingested["version"] == 1 and ingested["status"] == "ready"
    first = json_result(run_cli(tmp_path, "knowledge", "build", "--database", database))
    found = json_result(run_cli(tmp_path, "knowledge", "search", "Alpha", "--database", database))
    relations = tmp_path / "relations.json"
    relations.write_text(
        json.dumps(
            [
                {
                    "subject": "Alpha",
                    "predicate": "USES",
                    "object": "Beta",
                    "source_ids": [found["evidence"][0]["source_id"]],
                }
            ]
        ),
        encoding="utf-8",
    )
    graph = json_result(
        run_cli(
            tmp_path,
            "knowledge",
            "build",
            "--with-graph",
            "--relations",
            relations,
            "--database",
            database,
        )
    )
    assert graph["artifacts"]["graph"]["fact_count"] == 1
    document.write_text("# 设备记录\n\nAlpha 使用 Gamma。\n", encoding="utf-8")
    changed = json_result(
        run_cli(
            tmp_path, "knowledge", "ingest", document, "--title", "设备记录", "--database", database
        )
    )
    assert changed["version"] == 2
    json_result(run_cli(tmp_path, "knowledge", "build", "--database", database))
    historical = json_result(
        run_cli(
            tmp_path,
            "knowledge",
            "search",
            "Alpha",
            "--database",
            database,
            "--snapshot-id",
            first["id"],
        )
    )
    assert "Beta" in historical["evidence"][0]["text"]
    assert historical["evidence"][0]["document_version"] == 1


def test_failed_document_retry_uses_saved_version_without_replacing_source(tmp_path):
    database = tmp_path / "retry.db"
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    failed = run_cli(
        tmp_path, "knowledge", "ingest", invalid, "--title", "无法解码", "--database", database
    )
    assert failed.returncode == 2
    listing = json_result(run_cli(tmp_path, "knowledge", "list", "--database", database))
    version = listing["documents"][0]
    assert version["status"] == "failed"
    retried = run_cli(
        tmp_path,
        "knowledge",
        "retry",
        version["document_id"],
        version["version"],
        "--database",
        database,
    )
    assert retried.returncode == 2
    after = json_result(run_cli(tmp_path, "knowledge", "list", "--database", database))[
        "documents"
    ][0]
    assert (
        after["version"] == version["version"]
        and after["source_sha256"] == version["source_sha256"]
    )


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_missing_research_model_returns_nonzero_and_persists_selected_mode_and_snapshot(
    tmp_path, mode
):
    database = tmp_path / "research.db"
    loaded = json_result(run_cli(tmp_path, "knowledge", "load-demo", "--database", database))
    result = run_cli(
        tmp_path, "research", "X100 是否兼容 Y20？", "--mode", mode, "--database", database
    )
    assert result.returncode == 2
    assert "尚未配置语言模型" in result.stderr.decode("utf-8")
    with sqlite3.connect(database) as connection:
        run = connection.execute("SELECT status,request_context,error_json FROM runs").fetchone()
    assert run[0] == "failed"
    assert json.loads(run[1]) == {
        "research_mode": mode,
        "snapshot_id": loaded["loaded_snapshot_id"],
    }
    assert json.loads(run[2])["code"] == "SERVICE_NOT_CONFIGURED"


def test_evaluation_command_saves_real_json_and_rejects_unavailable_method(tmp_path):
    database = tmp_path / "evaluation.db"
    loaded = json_result(run_cli(tmp_path, "knowledge", "load-demo", "--database", database))
    output = tmp_path / "knowledge-results.json"
    summary = json_result(
        run_cli(tmp_path, "evaluate-knowledge", "--database", database, "--output", output)
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert summary["snapshot_id"] == loaded["loaded_snapshot_id"]
    assert report["status"] == "complete" and report["dataset"]["case_count"] == 28
    failed_output = tmp_path / "unavailable-results.json"
    unavailable = run_cli(
        tmp_path,
        "evaluate-knowledge",
        "--database",
        database,
        "--output",
        failed_output,
        "--methods",
        "lexical,semantic",
    )
    assert unavailable.returncode == 2 and not failed_output.exists()
    original = tmp_path / "evaluation.json"
    original.write_text("protected historical report", encoding="utf-8")
    protected = run_cli(
        tmp_path, "evaluate-knowledge", "--database", database, "--output", original
    )
    assert (
        protected.returncode == 2
        and original.read_text(encoding="utf-8") == "protected historical report"
    )


def test_lifecycle_closes_real_services_when_command_fails(tmp_path, monkeypatch, capsys):
    for key in tuple(os.environ):
        if key.startswith("TRACEDESK_"):
            monkeypatch.delenv(key)
    original = Services.close
    closed = []

    async def observed_close(self):
        closed.append(self.settings.database_path)
        await original(self)

    monkeypatch.setattr(Services, "close", observed_close)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tracedesk",
            "knowledge",
            "retry",
            "missing",
            "1",
            "--database",
            str(tmp_path / "closed.db"),
        ],
    )
    assert cli.main() == 2
    assert closed == [tmp_path / "closed.db"]
    assert "配置或执行错误" in capsys.readouterr().err
