"""The knock protocol, client side.

A door refuses a write with 403 and a remedy. Until 2026-09-18 nothing in this
repo implemented that remedy, so a gated channel refused every first-party
writer -- including the owner's own console -- for nineteen days.

These pin the parts that make the client honest:
  * the pass is presented on EVERY write path, not just `send`;
  * it is fetched once and reused while it is valid;
  * a stale pass is re-presented exactly once, never in a loop;
  * with no evidence to present, the client still sends -- the RELAY decides
    whether that is allowed. A client must never conclude it is exempt;
  * a refusal at the door names the one thing a human must do.
"""
from __future__ import annotations

import httpx
import pytest

from awrelay.client import RelayClient, RelayError


class _Doors:
    """Stands in for the gateway's /doors/present."""

    def __init__(self, tokens, status=200):
        self.tokens = list(tokens)
        self.status = status
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "no human check presented"})
        token = self.tokens.pop(0) if self.tokens else "pass-final"
        return httpx.Response(200, json={
            "admitted": True, "attestation": token,
            "expires_at": 4102444800,  # far future; expiry is covered separately
        })


def _client(monkeypatch, doors, *, humanity="awn1.humanity", relay_handler=None):
    """A RelayClient whose relay transport and doors transport are both fakes."""
    seen = []

    def default_relay(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "m1"})

    handler = relay_handler or default_relay
    c = RelayClient(
        "https://relay.test", token="tok", nick="david",
        humanity_source=(lambda: humanity),
    )
    c._client = httpx.Client(base_url="https://relay.test",
                             transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: doors(
        httpx.Request("POST", url, json=kw.get("json"))))
    return c, seen


def test_send_presents_the_pass(monkeypatch):
    doors = _Doors(["pass-1"])
    c, seen = _client(monkeypatch, doors)
    c.send_text("#agents", "hello")
    assert seen[0].headers.get("X-Door-Attestation") == "pass-1"


def test_thread_reply_presents_the_pass_too(monkeypatch):
    """The 2026-09-18 hole was exactly this: the message route gated, the
    thread route not. A client that only knocks on one is the mirror image."""
    doors = _Doors(["pass-1"])
    c, seen = _client(monkeypatch, doors)
    c.reply_in_thread("#agents", "m0", "a reply")
    assert seen[0].headers.get("X-Door-Attestation") == "pass-1"


def test_pass_is_fetched_once_and_reused(monkeypatch):
    doors = _Doors(["pass-1", "pass-2"])
    c, seen = _client(monkeypatch, doors)
    c.send_text("#agents", "one")
    c.send_text("#agents", "two")
    assert doors.calls == 1, "the door was knocked at twice for one valid pass"
    assert [r.headers.get("X-Door-Attestation") for r in seen] == ["pass-1", "pass-1"]


def test_a_stale_pass_is_represented_once_then_the_refusal_stands(monkeypatch):
    doors = _Doors(["stale", "fresh"])
    attempts = []

    def relay(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("X-Door-Attestation"))
        if len(attempts) == 1:
            return httpx.Response(403, json={"detail": "#agents is behind the door"})
        return httpx.Response(200, json={"id": "m1"})

    c, _ = _client(monkeypatch, doors, relay_handler=relay)
    c.send_text("#agents", "hello")
    assert attempts == ["stale", "fresh"], attempts
    assert doors.calls == 2


def test_no_retry_loop_when_the_door_keeps_issuing_the_same_pass(monkeypatch):
    doors = _Doors(["same", "same", "same"])
    attempts = []

    def relay(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("X-Door-Attestation"))
        return httpx.Response(403, json={"detail": "behind the door"})

    c, _ = _client(monkeypatch, doors, relay_handler=relay)
    with pytest.raises(RelayError):
        c.send_text("#agents", "hello")
    assert len(attempts) == 1, f"retried on an identical pass: {attempts}"


def test_without_evidence_the_client_still_sends_and_lets_the_relay_decide(monkeypatch):
    """A client must not decide it is exempt. With nothing to present it sends
    bare; an exempt caller class is admitted by the SERVER, and a gated one
    gets the server's own 403."""
    doors = _Doors([])
    c, seen = _client(monkeypatch, doors, humanity=None)
    c.send_text("#agents", "hello")
    assert doors.calls == 0
    assert "X-Door-Attestation" not in seen[0].headers


def test_door_refusal_names_what_the_human_must_do(monkeypatch):
    doors = _Doors([], status=403)
    c, _ = _client(monkeypatch, doors)
    with pytest.raises(RelayError) as exc:
        c.send_text("#agents", "hello")
    msg = str(exc.value)
    assert "verify-humanity" in msg, msg
    assert "30 days" in msg, msg


def test_an_expired_cached_pass_is_refreshed(monkeypatch):
    doors = _Doors(["pass-1", "pass-2"])
    c, seen = _client(monkeypatch, doors)
    c.send_text("#agents", "one")
    # Age the cache out rather than sleeping.
    c._door_cache["#agents"] = (c._door_cache["#agents"][0], 0.0)
    c.send_text("#agents", "two")
    assert [r.headers.get("X-Door-Attestation") for r in seen] == ["pass-1", "pass-2"]


def test_an_unreachable_doors_plane_does_not_break_an_ungated_channel(monkeypatch):
    """Most channels have no door. A doors plane that is down must not stop
    posting to them."""
    def boom(url, **kw):
        raise httpx.ConnectError("gateway down")

    c, seen = _client(monkeypatch, _Doors([]))
    monkeypatch.setattr(httpx, "post", boom)
    c.send_text("#general", "hello")
    assert "X-Door-Attestation" not in seen[0].headers
