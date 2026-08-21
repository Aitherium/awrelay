"""A2ABridge tests: no live AitherA2A required, httpx.MockTransport stands
in for one. Companion to test_standalone.py's RelayClient tests.
"""

from __future__ import annotations

# isort: skip_file
# See test_standalone.py's module docstring for why this file needs it too.
import json

import httpx
import pytest

from awrelay.a2a_bridge import (
    A2A_TOKEN_HEADER,
    A2ABridge,
    A2AApprovalPendingError,
    A2AError,
    bridge_and_reply,
)
from awrelay.client import RelayClient
from awrelay.envelope import Envelope


def _mock_bridge(handler) -> A2ABridge:
    bridge = A2ABridge("https://a2a.example")
    bridge._client = httpx.Client(
        base_url="https://a2a.example", transport=httpx.MockTransport(handler)
    )
    return bridge


def test_never_sends_an_internal_key_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"status": "success"})

    bridge = _mock_bridge(handler)
    bridge.call("AitherWorkingMemory", "save", {"content": "x"})

    lowered = {k.lower() for k in seen["headers"]}
    assert "x-internal-key" not in lowered
    assert "authorization" not in lowered  # this bridge only ever sends the grant header


def test_call_posts_the_real_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "success", "result": {"ok": True}})

    bridge = _mock_bridge(handler)
    result = bridge.call("AitherWorkingMemory", "save", {"content": "hello"})

    assert seen["url"].endswith("/call")
    assert seen["body"] == {
        "service": "AitherWorkingMemory", "skill": "save", "params": {"content": "hello"},
    }
    assert result["result"]["ok"] is True


def test_delegate_posts_the_real_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "success"})

    bridge = _mock_bridge(handler)
    bridge.delegate("artist", "draw a diagram", from_agent="demiurge", session_id="s1")

    assert seen["body"] == {
        "from": "demiurge", "to": "artist", "message": "draw a diagram", "sessionId": "s1",
    }


def test_403_with_a_card_raises_approval_pending_naming_the_request_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": {
            "error": "not_authorized",
            "resource": "a2a.skill.AitherWorkingMemory.save:execute",
            "access_request_id": "req-42",
            "how_to_proceed": "A human must approve this request.",
        }})

    bridge = _mock_bridge(handler)
    with pytest.raises(A2AApprovalPendingError) as exc_info:
        bridge.call("AitherWorkingMemory", "save")

    assert exc_info.value.access_request_id == "req-42"
    assert exc_info.value.resource == "a2a.skill.AitherWorkingMemory.save:execute"


def test_403_without_a_card_raises_a2a_error_not_approval_pending():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    bridge = _mock_bridge(handler)
    with pytest.raises(A2AError):
        bridge.call("AitherWorkingMemory", "save")


def test_500_raises_a2a_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    bridge = _mock_bridge(handler)
    with pytest.raises(A2AError):
        bridge.call("AitherWorkingMemory", "save")


def test_set_grant_sends_the_token_on_the_matching_resource_only():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(A2A_TOKEN_HEADER))
        return httpx.Response(200, json={"status": "success"})

    bridge = _mock_bridge(handler)
    bridge.set_grant("a2a.skill.AitherWorkingMemory.save:execute", "grant-abc")

    bridge.call("AitherWorkingMemory", "save")  # matching resource -> token sent
    bridge.call("AitherWorkingMemory", "delete")  # different skill -> no token

    assert seen == ["grant-abc", None]


def test_call_envelope_maps_service_skill_payload_to_call():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/call")
        return httpx.Response(200, json={"status": "success"})

    bridge = _mock_bridge(handler)
    env = Envelope.new(
        "request", "agent-a", "save this",
        payload={"service": "AitherWorkingMemory", "skill": "save", "params": {"x": 1}},
    )
    bridge.call_envelope(env)


def test_call_envelope_maps_to_only_payload_to_delegate():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/delegate")
        return httpx.Response(200, json={"status": "success"})

    bridge = _mock_bridge(handler)
    env = Envelope.new("request", "agent-a", "draw this", payload={"to": "artist"})
    bridge.call_envelope(env)


def test_call_envelope_rejects_non_request_kinds():
    bridge = _mock_bridge(lambda r: httpx.Response(200, json={}))
    env = Envelope.new("finding", "agent-a", "found a bug")
    with pytest.raises(ValueError):
        bridge.call_envelope(env)


def test_call_envelope_rejects_a_payload_with_neither_shape():
    bridge = _mock_bridge(lambda r: httpx.Response(200, json={}))
    env = Envelope.new("request", "agent-a", "do something", payload={"nonsense": True})
    with pytest.raises(ValueError):
        bridge.call_envelope(env)


def test_bridge_and_reply_posts_the_card_into_the_channel_on_pending():
    def a2a_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": {
            "resource": "a2a.skill.AitherWorkingMemory.save:execute",
            "access_request_id": "req-99",
            "how_to_proceed": "Approve via /access-requests/req-99/approve.",
        }})

    seen_relay = {}

    def relay_handler(request: httpx.Request) -> httpx.Response:
        seen_relay["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "m1"})

    bridge = _mock_bridge(a2a_handler)
    relay = RelayClient("https://relay.example", token="tok", nick="bridge-agent")
    relay._client = httpx.Client(
        base_url="https://relay.example", transport=httpx.MockTransport(relay_handler)
    )

    env = Envelope.new(
        "request", "agent-a", "save this",
        payload={"service": "AitherWorkingMemory", "skill": "save"},
        correlation_id="corr-1",
    )
    result = bridge_and_reply(bridge, relay, "#agent-lounge", env)

    assert isinstance(result, A2AApprovalPendingError)
    assert result.access_request_id == "req-99"
    assert "req-99" in seen_relay["body"]["content"] or "req-99" in json.dumps(seen_relay["body"])


def test_bridge_and_reply_returns_the_result_on_success():
    bridge = _mock_bridge(lambda r: httpx.Response(200, json={"status": "success", "ok": True}))
    relay = RelayClient("https://relay.example", token="tok", nick="bridge-agent")
    relay._client = httpx.Client(
        base_url="https://relay.example",
        transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"id": "m1"})),
    )
    env = Envelope.new(
        "request", "agent-a", "save this",
        payload={"service": "AitherWorkingMemory", "skill": "save"},
    )
    result = bridge_and_reply(bridge, relay, "#agent-lounge", env)
    assert result == {"status": "success", "ok": True}
