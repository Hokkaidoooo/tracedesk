import asyncio
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from tracedesk.bootstrap import create_services
from tracedesk.errors import DomainError
from tracedesk.provider import ChatCompletionsProvider, parse_reply
from tracedesk.settings import Settings


def completion(name="final_answer", arguments=None):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "reasoning_content": "private-provider-field",
                    "tool_calls": [
                        {
                            "id": str(uuid4()),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    arguments
                                    or {"status": "needs_information", "clarification": "order_id"}
                                ),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"total_tokens": 42},
    }


@contextmanager
def server(respond):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed.append(
                {
                    "path": self.path,
                    "body": payload,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            respond(self, payload, len(observed))

        def log_message(self, *args):
            return

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=service.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{service.server_port}/v1", observed
    finally:
        service.shutdown()
        service.server_close()
        worker.join(timeout=3)


def send(handler, value, status=200):
    content = json.dumps(value).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


@pytest.mark.parametrize(
    "body",
    [
        [],
        42,
        {},
        {"choices": [42]},
        {"choices": []},
        {"choices": [{"finish_reason": "tool_calls", "message": []}]},
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "tool_calls": [42]},
                }
            ]
        },
        {"choices": [{"finish_reason": "length", "message": {}}]},
    ],
)
def test_malformed_provider_json_is_a_protocol_error(body):
    with pytest.raises(DomainError) as error:
        parse_reply(json.dumps(body).encode())
    assert error.value.code == "MODEL_PROTOCOL_ERROR"


def test_parser_does_not_forward_private_reasoning():
    parsed = parse_reply(json.dumps(completion()).encode())
    assert parsed.total_tokens == 42
    assert "private-provider-field" not in json.dumps(parsed.message)
    assert "reasoning_content" not in parsed.message


def test_duplicate_call_ids_are_a_protocol_error():
    body = completion()
    calls = body["choices"][0]["message"]["tool_calls"]
    calls.append(dict(calls[0]))
    with pytest.raises(DomainError) as error:
        parse_reply(json.dumps(body).encode())
    assert error.value.code == "MODEL_PROTOCOL_ERROR"


def test_real_http_transport_and_agent_tool_round_trip(tmp_path):
    """This tests actual HTTP against a declared fixture server, not a real model score."""

    def respond(handler, payload, count):
        if count == 1:
            send(handler, completion("lookup_order", {"order_id": "TD-1001"}))
        else:
            send(handler, completion("final_answer", {"status": "answered"}))

    async def execute(base_url):
        settings = Settings(
            provider="llm",
            base_url=base_url,
            api_key="fixture-not-a-real-key",
            database_path=tmp_path / "wire.db",
        )
        provider = ChatCompletionsProvider(settings)
        try:
            services = create_services(
                settings,
                provider=provider,
                clock=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
            )
            created = services.store.create_session("customer-a")
            session = services.store.authenticate(created["token"])
            return await services.agent.ask(session, "查询 TD-1001", str(uuid4()))
        finally:
            await provider.close()

    with server(respond) as (base_url, observed):
        turn = asyncio.run(execute(base_url))
    assert turn["status"] == "answered" and turn["runtime"] == "llm"
    assert "129.00" in turn["answer"]
    assert len(observed) == 2
    assert observed[0]["path"] == "/v1/chat/completions"
    assert observed[0]["authorization"] == "Bearer fixture-not-a-real-key"
    assert observed[0]["body"]["tool_choice"] == "required"
    assert any(message["role"] == "tool" for message in observed[1]["body"]["messages"])


def test_slow_stream_cannot_evade_total_request_deadline():
    completed = threading.Event()

    def respond(handler, payload, count):
        content = json.dumps(completion()).encode()
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(content)))
        handler.end_headers()
        try:
            for offset in range(0, len(content), 20):
                handler.wfile.write(content[offset : offset + 20])
                handler.wfile.flush()
                time.sleep(0.04)
            completed.set()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return  # The expected client deadline closed this test connection.

    async def execute(base_url):
        provider = ChatCompletionsProvider(
            Settings(provider="llm", base_url=base_url, request_timeout_seconds=0.15)
        )
        try:
            await provider.complete([{"role": "user", "content": "test"}], [])
        finally:
            await provider.close()

    with server(respond) as (base_url, _):
        with pytest.raises(DomainError) as error:
            asyncio.run(execute(base_url))
        assert error.value.code == "MODEL_TIMEOUT"
        assert not completed.is_set()


def test_upstream_error_body_is_not_exposed():
    def respond(handler, payload, count):
        send(handler, {"error": "sensitive-upstream-diagnostic"}, 401)

    async def execute(base_url):
        provider = ChatCompletionsProvider(Settings(provider="llm", base_url=base_url))
        try:
            await provider.complete([], [])
        finally:
            await provider.close()

    with server(respond) as (base_url, _):
        with pytest.raises(DomainError) as error:
            asyncio.run(execute(base_url))
    assert error.value.code == "MODEL_HTTP_ERROR"
    assert "401" in error.value.message
    assert "sensitive-upstream" not in str(error.value)


@pytest.mark.parametrize(
    "settings",
    [
        {"request_timeout_seconds": float("nan")},
        {"request_timeout_seconds": float("inf")},
        {"run_lease_seconds": 1},
        {"max_tool_calls": 0},
        {"provider": "llm", "base_url": "http://example.invalid/v1"},
        {
            "provider": "llm",
            "base_url": "https://user:password@example.invalid/v1",
            "api_key": "fixture",
        },
        {
            "provider": "llm",
            "base_url": "https://example.invalid/v1?token=fixture",
            "api_key": "fixture",
        },
        {"provider": "unknown"},
        {"demo_mode": False},
    ],
)
def test_invalid_configuration_fails_before_network(settings):
    with pytest.raises(ValueError):
        Settings(**settings)


def test_settings_repr_omits_api_key():
    assert "fixture-value" not in repr(Settings(api_key="fixture-value"))
