"""Constructing a client must not build an httpx.Client (and its TLS trust store).

Measured on a Windows host with truststore: every ``httpx.Client(verify=True)``
costs 1-2 s, and the suite built one per test only to replace it with a
MockTransport client -- the whole run was killed at 240 s. The client is now
built on first use, and a ``transport`` can be injected instead.
"""

from __future__ import annotations

import httpx
import pytest

import awrelay.a2a_bridge as a2a_mod
import awrelay.client as client_mod
from awrelay.a2a_bridge import A2ABridge
from awrelay.client import RelayClient


class _BuiltError(Exception):
    pass


@pytest.fixture()
def no_real_clients(monkeypatch: pytest.MonkeyPatch) -> list:
    built: list = []
    real = httpx.Client

    def _guard(*args, **kwargs):
        if "transport" not in kwargs:
            built.append(kwargs)
            raise _BuiltError("a real (TLS) httpx.Client was built")
        return real(*args, **kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", _guard)
    monkeypatch.setattr(a2a_mod.httpx, "Client", _guard)
    return built


def test_relay_client_construction_builds_no_http_client(no_real_clients: list) -> None:
    rc = RelayClient("https://relay.example", token="t", nick="n")
    rc.close()  # closing a never-used client must not build one either
    assert no_real_clients == []


def test_a2a_bridge_construction_builds_no_http_client(no_real_clients: list) -> None:
    bridge = A2ABridge("https://a2a.example")
    bridge.close()
    assert no_real_clients == []


def test_injected_transport_is_used_without_a_tls_client(no_real_clients: list) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"channels": []})

    rc = RelayClient("https://relay.example", token="t", nick="n",
                     transport=httpx.MockTransport(handler))
    rc._client.get("/v1/channels")
    assert seen["url"] == "https://relay.example/v1/channels"
    assert no_real_clients == []


def test_first_use_still_builds_a_verifying_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(client_mod.httpx, "Client", _Fake)
    rc = RelayClient("https://relay.example", verify="/ca.pem")
    assert captured == {}
    rc._client  # noqa: B018 - first access builds it
    assert captured["verify"] == "/ca.pem"
    assert captured["base_url"] == "https://relay.example"
