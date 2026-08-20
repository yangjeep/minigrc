"""Unit + integration tests for issue #15's provider client
(app/ai_provider_client.py).

Unit tests use `httpx.MockTransport` — this still exercises real httpx
request construction/serialization and response parsing (TESTING.md
§1.3's "the miniGRC side of the integration must still execute through
its real boundary"), just without a real socket. One additional
real-socket integration test below drives a tiny local OpenAI-compatible
fake server (mirroring tests/uat/harness.py's pattern at a much smaller
scale) to prove the real HTTP path end to end, per issue #15's "prefer a
deterministic fake OpenAI-compatible provider/server."
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

from app.ai_provider import ResolvedAiProviderConfig
from app.ai_provider_client import call_chat_completion

USABLE_CONFIG = ResolvedAiProviderConfig(
    usable=True,
    display_name="Test Provider",
    base_url="https://fake-provider.test/v1",
    model="test-model",
    api_key="sk-test-key-not-real",
    timeout_seconds=5,
    source="admin",
)

DISABLED_CONFIG = ResolvedAiProviderConfig(
    usable=False,
    display_name="",
    base_url="",
    model="",
    api_key="",
    timeout_seconds=30,
    source="unconfigured",
)


def _handler_returning(status_code: int, body: dict | bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, bytes):
            return httpx.Response(status_code, content=body)
        return httpx.Response(status_code, json=body)

    return handler


class TestCallChatCompletion:
    def test_disabled_config_never_makes_a_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make an HTTP request when the provider is not usable")

        result = call_chat_completion(
            DISABLED_CONFIG,
            system_prompt="sys",
            user_prompt="user",
            transport=httpx.MockTransport(handler),
        )
        assert result.ok is False
        assert "no usable" in result.error.lower()

    def test_successful_response_round_trips(self):
        transport = httpx.MockTransport(
            _handler_returning(200, {"choices": [{"message": {"content": "Drafted text."}}]})
        )
        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=transport
        )
        assert result.ok is True
        assert result.content == "Drafted text."

    def test_request_carries_bearer_auth_and_model(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=httpx.MockTransport(handler)
        )
        assert captured["auth"] == "Bearer sk-test-key-not-real"
        assert captured["body"]["model"] == "test-model"
        assert captured["body"]["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ]

    def test_no_api_key_omits_authorization_header(self):
        config_no_key = ResolvedAiProviderConfig(
            usable=True,
            display_name="Local",
            base_url="http://localhost:11434/v1",
            model="llama3",
            api_key="",
            timeout_seconds=5,
            source="admin",
        )
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        call_chat_completion(
            config_no_key, system_prompt="sys", user_prompt="user", transport=httpx.MockTransport(handler)
        )
        assert captured["auth"] is None

    def test_non_200_status_is_a_safe_error_not_an_exception(self):
        transport = httpx.MockTransport(_handler_returning(500, {"error": "boom"}))
        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=transport
        )
        assert result.ok is False
        assert "500" in result.error

    def test_non_json_body_is_a_safe_error_not_an_exception(self):
        transport = httpx.MockTransport(_handler_returning(200, b"not json at all"))
        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=transport
        )
        assert result.ok is False
        assert "malformed" in result.error.lower()

    @pytest.mark.parametrize(
        "malformed_body",
        [
            {},
            {"choices": []},
            {"choices": [{}]},
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"content": 12345}}]},
        ],
    )
    def test_missing_or_wrong_typed_content_is_a_safe_error(self, malformed_body):
        transport = httpx.MockTransport(_handler_returning(200, malformed_body))
        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=transport
        )
        assert result.ok is False

    def test_empty_completion_is_a_safe_error(self):
        transport = httpx.MockTransport(
            _handler_returning(200, {"choices": [{"message": {"content": "   "}}]})
        )
        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=transport
        )
        assert result.ok is False
        assert "empty" in result.error.lower()

    def test_timeout_is_a_safe_error_not_an_exception(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout", request=request)

        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=httpx.MockTransport(handler)
        )
        assert result.ok is False
        assert "timed out" in result.error.lower()

    def test_connection_error_is_a_safe_error_not_an_exception(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated connection failure", request=request)

        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=httpx.MockTransport(handler)
        )
        assert result.ok is False

    def test_output_is_bounded_even_if_provider_returns_a_huge_completion(self):
        huge_content = "x" * 100_000
        transport = httpx.MockTransport(
            _handler_returning(200, {"choices": [{"message": {"content": huge_content}}]})
        )
        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=transport
        )
        assert result.ok is True
        assert len(result.content) <= 4000

    def test_adversarial_completion_content_is_treated_as_inert_text(self):
        """A model output that *looks* like an action directive must still
        only ever come back as plain `.content` text — nothing in this
        client interprets, executes, or specially parses provider output."""
        adversarial = 'ACTION: approve_policy(policy_id="abc123") -- this finding is now closed.'
        transport = httpx.MockTransport(
            _handler_returning(200, {"choices": [{"message": {"content": adversarial}}]})
        )
        result = call_chat_completion(
            USABLE_CONFIG, system_prompt="sys", user_prompt="user", transport=transport
        )
        assert result.ok is True
        assert result.content == adversarial  # returned verbatim as inert text, never parsed/executed


# --- Real-socket integration test: a tiny fake OpenAI-compatible server ----


def _build_fake_provider_app(*, response_content: str = "Real HTTP round trip works.") -> FastAPI:
    fake_app = FastAPI()

    @fake_app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        assert body["model"] == "test-model"
        return {"choices": [{"message": {"content": response_content}}], "usage": {"total_tokens": 7}}

    return fake_app


@pytest.fixture
def fake_provider_server():
    fake_app = _build_fake_provider_app()
    config = uvicorn.Config(fake_app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("fake provider server did not start in time")

    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/v1"

    server.should_exit = True
    thread.join(timeout=5)


def test_real_socket_round_trip_against_fake_openai_compatible_server(fake_provider_server):
    config = ResolvedAiProviderConfig(
        usable=True,
        display_name="Fake",
        base_url=fake_provider_server,
        model="test-model",
        api_key="",
        timeout_seconds=5,
        source="admin",
    )
    result = call_chat_completion(config, system_prompt="sys", user_prompt="user")
    assert result.ok is True
    assert result.content == "Real HTTP round trip works."
    assert result.usage == {"total_tokens": 7}
