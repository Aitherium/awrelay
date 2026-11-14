"""A person on the team talks from a terminal: tail, dm, team, channels --no-sessions.

One-surface slice 1 (2026-11-13): Jason, on his own computer, runs four commands and is
talking with David under his own name. Before 0.5.3 the CLI had no default URL (the README
named irc.aitherium.com, which answers 301), no way to follow a room, DM a person or list
the team, and 887 of 977 visible channels were per-session rooms.
"""
from __future__ import annotations

import json

import httpx
import pytest

#: The brick under test. Imported after a statement so the brick's own isort config
#: (awrelay first-party) and the repo gate's isolated isort (third-party) agree.
BRICK = "awrelay"
from awrelay import cli  # noqa: E402
from awrelay.client import RelayClient  # noqa: E402


class FakeRelay:
    def __init__(self):
        self.messages = [{"id": "1", "nick": "david", "content": "morning",
                          "timestamp": "2026-11-13T09:00:01"}]
        self.dms = []
        self.members = [{"nick": "jason", "role": "member", "online": False},
                        {"nick": "david", "role": "admin", "online": True}]
        self.requests = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path.startswith("/api/relay/"):          # the public base carries a prefix
            path = path[len("/api/relay"):]
        if path == "/v1/channels":
            return httpx.Response(200, json={"channels": [
                {"name": "#aitherium-general"}, {"name": "#session-abc123"},
                {"name": "#aitherium-builds"}]})
        if path.endswith("/messages") and req.method == "GET":
            return httpx.Response(200, json={"messages": list(self.messages)})
        if path == "/v1/dms" and req.method == "POST":
            body = json.loads(req.content)
            self.dms.append(body)
            return httpx.Response(200, json={"success": True})
        if path.startswith("/v1/dms/"):
            return httpx.Response(200, json={"messages": [
                {"from_nick": "david", "content": "hi jason"}]})
        if path == "/v1/workspaces/aitherium/members":
            return httpx.Response(200, json={"members": self.members})
        return httpx.Response(404, json={"detail": path})


@pytest.fixture
def relay(monkeypatch):
    fake = FakeRelay()
    real_init = RelayClient.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(fake)
        real_init(self, *a, **kw)

    monkeypatch.setattr(RelayClient, "__init__", init)
    monkeypatch.setenv("AWRELAY_TOKEN", "tok")
    monkeypatch.setenv("AWRELAY_NICK", "jason")
    monkeypatch.delenv("AWRELAY_URL", raising=False)
    return fake


def test_the_public_relay_is_the_default(relay):
    assert cli.main(["channels"]) == 0
    assert str(relay.requests[0].url).startswith(cli.PUBLIC_RELAY_URL)
    assert "irc.aitherium.com" not in cli.PUBLIC_RELAY_URL


def test_channels_can_hide_session_rooms(relay, capsys):
    assert cli.main(["channels", "--no-sessions"]) == 0
    out = capsys.readouterr().out
    assert "#aitherium-general" in out and "#session-abc123" not in out


def test_team_lists_the_roster_online_first(relay, capsys):
    assert cli.main(["team"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("* david") and "admin" in lines[0]
    assert "jason" in lines[1]


def test_dm_sends_to_the_named_person(relay):
    assert cli.main(["dm", "david", "ready when you are"]) == 0
    assert relay.dms == [{"to_nick": "david", "content": "ready when you are"}]


def test_dm_without_text_shows_the_thread(relay, capsys):
    assert cli.main(["dm", "david"]) == 0
    assert "david: hi jason" in capsys.readouterr().out


def test_tail_prints_the_team_room_backlog_and_new_messages(relay, capsys, monkeypatch):
    calls = {"n": 0}
    real_history = RelayClient.history

    def history(self, channel, **kw):
        calls["n"] += 1
        assert channel == "#aitherium-general"            # the team room by default
        if calls["n"] == 2:
            relay.messages.append({"id": "2", "nick": "jason", "content": "on it"})
        return real_history(self, channel, **kw)

    monkeypatch.setattr(RelayClient, "history", history)
    clock = iter([0.0, 0.0, 0.5, 2.0, 5.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["tail", "--seconds", "1", "--interval", "0"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["09:00:01 david: morning", "jason: on it"]   # each line once


def test_tail_reports_a_refusal_as_exit_1(monkeypatch, capsys):
    def deny(req):
        return httpx.Response(403, json={"detail": "not a member"})

    real_init = RelayClient.__init__
    monkeypatch.setattr(RelayClient, "__init__", lambda self, *a, **kw: real_init(
        self, *a, **{**kw, "transport": httpx.MockTransport(deny)}))
    monkeypatch.setenv("AWRELAY_TOKEN", "tok")
    monkeypatch.setenv("AWRELAY_NICK", "jason")
    assert cli.main(["tail", "--seconds", "0"]) == 1
    assert "403" in capsys.readouterr().err


def test_tail_shows_an_envelope_as_its_words():
    from awrelay.envelope import Envelope

    env = Envelope.new("message", "david+3f2a", "hello team")
    line = cli._line({"nick": "david+3f2a", "content": env.to_relay_content(),
                      "timestamp": "2026-11-13T14:15:34"})
    assert line == "14:15:34 david+3f2a: hello team"
