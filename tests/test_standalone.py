"""Self-contained tests: no live relay server required. `RelayClient` is
exercised against an httpx MockTransport standing in for one.
"""

from __future__ import annotations

# isort: skip_file
#
# The repo's ambient ruff config knows `awrelay` as FIRST-party and wants it in
# its own block; the quality gate runs ruff `--isolated` (deliberately, so the
# ambient per-file-ignores cannot hide E501/F-rules) where `awrelay` reads as
# THIRD-party and belongs beside httpx/pytest. Those two layouts are mutually
# exclusive - one block or two - so no ordering satisfies both and this states
# the conflict instead of flip-flopping the file every time whichever gate ran
# last disagreed. Same fix as awgraph/tests/test_plugins.py.
import json

import httpx
import pytest

from awrelay.client import RelayClient, RelayError, _path_segment
from awrelay.envelope import Envelope, Kind


def test_path_segment_encodes_the_hash_channel_prefix():
    # '#' is a URL FRAGMENT delimiter, not a literal path character — an
    # unencoded channel name silently truncates the request path. This is
    # the direct regression test for that bug.
    assert _path_segment("#agent-lounge") == "%23agent-lounge"
    assert "#" not in _path_segment("#agent-lounge")


def test_envelope_requires_text():
    with pytest.raises(ValueError):
        Envelope.new("message", "agent-a", "   ")


def test_envelope_round_trip():
    env = Envelope.new(
        "finding", "agent-a", "found a race condition",
        payload={"file": "x.py", "line": 12}, correlation_id="req-1",
    )
    content = env.to_relay_content()
    assert content.startswith("[finding] found a race condition")
    assert "```awrelay" in content

    back = Envelope.from_relay_content(content)
    assert back is not None
    assert back.kind == Kind.FINDING
    assert back.sender == "agent-a"
    assert back.text == "found a race condition"
    assert back.payload == {"file": "x.py", "line": 12}
    assert back.correlation_id == "req-1"


def test_from_relay_content_ignores_ordinary_chat():
    assert Envelope.from_relay_content("just saying hi") is None
    assert Envelope.from_relay_content("") is None


def test_from_relay_content_ignores_malformed_fence():
    # A fence present but not valid JSON must not raise — a stray
    # ```awrelay block from something else entirely is a mismatch, not a
    # crash.
    assert Envelope.from_relay_content("hi\n```awrelay\nnot json\n```") is None


def _mock_client(handler) -> RelayClient:
    client = RelayClient("https://relay.example", token="tok", nick="agent-a")
    client._client = httpx.Client(
        base_url="https://relay.example",
        transport=httpx.MockTransport(handler),
    )
    return client


def test_send_posts_the_envelope_and_returns_the_server_record():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "m1", "ok": True})

    client = _mock_client(handler)
    env = Envelope.new("message", "agent-a", "hello")
    result = client.send("#agent-lounge", env)

    assert result == {"id": "m1", "ok": True}
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/v1/channels/%23agent-lounge/messages"), seen["url"]
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["nick"] == "agent-a"
    assert "hello" in seen["body"]["content"]


def test_send_raises_relay_error_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = _mock_client(handler)
    with pytest.raises(RelayError):
        client.send("#agent-lounge", Envelope.new("message", "agent-a", "hi"))


def test_history_filters_to_envelopes_only():
    plain = {"nick": "human1", "content": "hey everyone"}
    enveloped = {
        "nick": "agent-a",
        "content": Envelope.new("finding", "agent-a", "found it").to_relay_content(),
    }
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"messages": [plain, enveloped]})

    client = _mock_client(handler)
    all_items = list(client.history("#agent-lounge"))
    assert len(all_items) == 2
    assert seen_urls[0].startswith("https://relay.example/v1/channels/%23agent-lounge/messages")

    only_envelopes = list(client.history("#agent-lounge", envelopes_only=True))
    assert len(only_envelopes) == 1
    assert isinstance(only_envelopes[0], Envelope)
    assert only_envelopes[0].text == "found it"


def test_channels_unwraps_either_response_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"channels": ["#a", "#b"]})

    client = _mock_client(handler)
    assert client.channels() == ["#a", "#b"]
