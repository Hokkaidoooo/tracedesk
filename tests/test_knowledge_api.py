import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tracedesk.api import create_app
from tracedesk.settings import Settings


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(database_path=tmp_path / "api.db"))) as value:
        yield value


def session(client: TestClient, index: int = 0) -> tuple[dict, dict]:
    customers = client.get("/api/demo/customers").json()["customers"]
    value = client.post("/api/sessions", json={"customer_id": customers[index]["id"]}).json()
    return value, {"Authorization": f"Bearer {value['token']}"}


def upload(client: TestClient, headers: dict, text: str):
    return client.post(
        "/api/knowledge/documents",
        headers={**headers, "Content-Type": "text/markdown"},
        params={"filename": "manual.md", "title": "测试产品说明"},
        content=text.encode(),
    )


def test_upload_publish_search_and_original_download(client: TestClient):
    _, headers = session(client)
    text = "# 测试产品说明\n\n测试主机支持外接显示器，但必须使用视频线。"
    response = upload(client, headers, text)
    assert response.status_code == 201, response.text
    document = response.json()
    snapshot = client.post("/api/knowledge/snapshots", headers=headers, json={}).json()
    result = client.post(
        "/api/knowledge/search",
        headers=headers,
        json={"query": "外接显示器", "snapshot_id": snapshot["id"]},
    )
    assert result.status_code == 200
    evidence = result.json()["evidence"]
    assert evidence and "必须使用视频线" in evidence[0]["text"]
    source = client.get(
        f"/api/knowledge/documents/{document['document_id']}/versions/1/source", headers=headers
    )
    assert source.content == text.encode()
    assert source.headers["x-source-sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert source.headers["content-disposition"].startswith("attachment;")
    assert client.get("/api/knowledge").status_code == 401
    assert evidence[0]["page"] is None


def test_uploaded_new_version_does_not_change_existing_snapshot(client: TestClient):
    _, headers = session(client)
    upload(client, headers, "第一版：Q10 配有两个视频接口，支持扩展显示器。")
    first = client.post("/api/knowledge/snapshots", headers=headers, json={}).json()["id"]
    second = upload(client, headers, "第二版：Q10 配有三个视频接口，支持扩展显示器。")
    assert second.json()["version"] == 2
    before = client.post(
        "/api/knowledge/search", headers=headers, json={"query": "视频接口"}
    ).json()
    assert before["snapshot_id"] == first
    assert all(item["document_version"] == 1 for item in before["evidence"])
    published = client.post("/api/knowledge/snapshots", headers=headers, json={}).json()["id"]
    assert published != first
    historical = client.post(
        "/api/knowledge/search",
        headers=headers,
        json={"query": "视频接口", "snapshot_id": first},
    ).json()
    assert historical["evidence"][0]["document_version"] == 1


def test_demo_graph_returns_real_stored_sources(client: TestClient):
    _, headers = session(client)
    loaded = client.post("/api/knowledge/demo", headers=headers, json={})
    assert loaded.status_code == 201, loaded.text
    body = loaded.json()
    assert body["methods"] == ["lexical", "graph"]
    snapshot_id = body["current_snapshot"]["id"]
    response = client.post(
        "/api/knowledge/search", headers=headers, json={"query": "X100", "method": "graph"}
    )
    assert response.status_code == 200, response.text
    hits = response.json()["evidence"]
    assert hits and any(len(item["graph_path"]) > 1 for item in hits)
    for item in hits:
        stored = client.get(
            f"/api/knowledge/snapshots/{snapshot_id}/evidence/{item['source_id']}", headers=headers
        )
        assert stored.status_code == 200
        assert stored.json()["source_sha256"] == item["source_sha256"]
        assert stored.json()["text"] == item["text"]


def test_unconfigured_research_is_recorded_as_failure(client: TestClient):
    current, headers = session(client)
    loaded = client.post("/api/knowledge/demo", headers=headers, json={}).json()
    request = {
        "message": "X100 能连接哪些扩展坞？",
        "request_id": str(uuid4()),
        "snapshot_id": loaded["current_snapshot"]["id"],
        "mode": "multi",
    }
    response = client.post(f"/api/sessions/{current['id']}/research", headers=headers, json=request)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_NOT_CONFIGURED"
    run = client.get(f"/api/runs/{error['details']['run_id']}", headers=headers).json()
    assert run["status"] == "failed"
    assert client.get(f"/api/sessions/{current['id']}", headers=headers).json()["turns"] == []
    repeat = client.post(f"/api/sessions/{current['id']}/research", headers=headers, json=request)
    assert repeat.json()["error"]["details"]["run_id"] == error["details"]["run_id"]
    changed = client.post(
        f"/api/sessions/{current['id']}/research",
        headers=headers,
        json={**request, "mode": "single"},
    )
    assert changed.status_code == 409


def test_semantic_request_never_silently_becomes_lexical(client: TestClient):
    _, headers = session(client)
    client.post("/api/knowledge/demo", headers=headers, json={})
    response = client.post(
        "/api/knowledge/search", headers=headers, json={"query": "显示器", "method": "semantic"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RETRIEVAL_UNAVAILABLE"


def test_failed_pdf_is_visible_and_same_title_does_not_become_plain_text(client: TestClient):
    _, headers = session(client)
    response = client.post(
        "/api/knowledge/documents",
        headers={**headers, "Content-Type": "application/pdf"},
        params={"filename": "manual.pdf", "title": "尚未解析的说明书"},
        content=b"%PDF-1.4\nfixture",
    )
    assert response.status_code >= 400
    documents = client.get("/api/knowledge", headers=headers).json()["documents"]
    assert len(documents) == 1 and documents[0]["status"] == "failed"
    snapshot = client.post("/api/knowledge/snapshots", headers=headers, json={})
    assert snapshot.status_code >= 400
    assert client.get("/api/knowledge", headers=headers).json()["current_snapshot"] is None
