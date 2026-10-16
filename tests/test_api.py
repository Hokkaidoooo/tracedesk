from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tracedesk.api import create_app
from tracedesk.settings import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_path=tmp_path / "api.db")
    app = create_app(settings, clock=lambda: datetime(2026, 10, 16, 8, tzinfo=timezone.utc))
    with TestClient(app) as test_client:
        yield test_client


def new_session(client, customer="customer-a"):
    response = client.post("/api/sessions", json={"customer_id": customer})
    assert response.status_code == 201
    session = response.json()
    return session, {"Authorization": f"Bearer {session['token']}"}


def turn(client, session, headers, message, request_id=None):
    return client.post(
        f"/api/sessions/{session['id']}/turns",
        headers=headers,
        json={"message": message, "request_id": request_id or str(uuid4())},
    )


def test_full_http_workflow_and_replay_show_current_action(client):
    session, headers = new_session(client)
    policy = turn(client, session, headers, "签收后多久可以退货？")
    assert policy.status_code == 200 and policy.json()["citations"][0]["source_id"] == "returns@2"
    request_id = str(uuid4())
    prepared = turn(client, session, headers, "申请退款 TD-1001", request_id)
    assert prepared.status_code == 200
    action = prepared.json()["action"]
    confirmed = client.post(
        f"/api/actions/{action['id']}/confirm",
        headers=headers,
        json={"confirmation_token": action["confirmation_token"], "request_id": str(uuid4())},
    )
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "submitted"
    replay = turn(client, session, headers, "申请退款 TD-1001", request_id).json()
    history = client.get(f"/api/sessions/{session['id']}", headers=headers).json()
    assert replay["id"] == prepared.json()["id"]
    assert replay["action"]["state"] == history["turns"][-1]["action"]["state"] == "submitted"
    assert "confirmation_token" not in replay["action"]
    stored = client.app.state.services.store.list_turns(
        client.app.state.services.store.authenticate(session["token"])
    )[-1]
    assert stored["action"]["state"] == "pending"  # The audit snapshot remains immutable.


def test_resources_require_the_owning_session(client):
    own, headers = new_session(client)
    other, other_headers = new_session(client, "customer-b")
    created = turn(client, own, headers, "申请退款 TD-1001").json()
    assert client.get(f"/api/sessions/{own['id']}").status_code == 401
    assert client.get(f"/api/sessions/{own['id']}", headers=other_headers).status_code in {401, 404}
    assert client.get(f"/api/runs/{created['run_id']}", headers=other_headers).status_code == 404
    action = created["action"]
    denied = client.post(
        f"/api/actions/{action['id']}/confirm",
        headers=other_headers,
        json={"confirmation_token": action["confirmation_token"], "request_id": str(uuid4())},
    )
    assert denied.status_code == 404
    assert turn(client, other, other_headers, "查询 TD-1001").json()["order"] is None


def test_invalid_request_is_structured_without_echoing_input(client):
    session, headers = new_session(client)
    response = client.post(
        f"/api/sessions/{session['id']}/turns",
        headers=headers,
        json={
            "message": "secret-user-input",
            "request_id": "not-a-uuid",
            "customer_id": "customer-b",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "secret-user-input" not in response.text


def test_health_info_schema_and_page_are_real(client):
    assert client.get("/health").json()["status"] == "ok"
    info = client.get("/api/info")
    assert info.json()["runtime"] == "extractive" and info.json()["model"] is None
    assert info.headers["Cache-Control"] == "no-store"
    schema = client.get("/openapi.json").json()
    assert "/api/actions/{action_id}/confirm" in schema["paths"]
    page = client.get("/")
    assert page.status_code == 200 and "TraceDesk" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]


def test_cancelled_action_remains_cancelled_on_http_replay(client):
    session, headers = new_session(client)
    request_id = str(uuid4())
    prepared = turn(client, session, headers, "申请退款 TD-1001", request_id).json()
    action_id = prepared["action"]["id"]
    response = client.post(f"/api/actions/{action_id}/cancel", headers=headers, json={})
    assert response.status_code == 200 and response.json()["state"] == "cancelled"
    replay = turn(client, session, headers, "申请退款 TD-1001", request_id).json()
    assert replay["action"]["state"] == "cancelled"
    assert "confirmation_token" not in replay["action"]
