"""The reading half of agent messaging: who am I, what is new for me, how it arrives."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import httpx
import pytest
from awrelay import cli, inbox, install
from awrelay.client import RelayClient, RelayError
from awrelay.envelope import Envelope

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp()
ME = "dana+a3b4c1d2"


def row(minute: int, nick: str, kind: str = "finding", text: str = "x", *, to=None,
        sender: str | None = None, raw: str | None = None, hour: int = 11) -> dict:
    payload = {"to": to} if to else {}
    env = Envelope.new(kind, nick if sender is None else sender, text, payload=payload)
    return {
        "id": f"m{hour}{minute:02d}", "nick": nick,
        "timestamp": f"2026-09-19T{hour:02d}:{minute:02d}:00.000000+00:00",
        "content": raw if raw is not None else env.to_relay_content(),
    }


def test_my_own_posts_and_machine_rows_are_never_delivered():
    rows = [
        row(10, ME, text="mine"),
        row(11, "awrun", text="probe"),
        row(12, "dana", raw="⟦mirror⟧ a prompt somebody typed"),
        row(13, "dana+ffff0000", text="peer finding"),
    ]
    got, cursor = inbox.select(rows, me=ME, identity="dana", now=NOW)
    assert [m["text"] for m in got] == ["peer finding"]
    # Skipped rows still advance the cursor, or a noisy channel is re-scanned forever.
    assert cursor == rows[-1]["timestamp"]


def test_chat_is_delivered_only_when_addressed_and_is_marked():
    rows = [
        row(20, "dana+ffff0000", kind="message", text="just chatting"),
        row(21, "dana+ffff0000", kind="message", text="to you", to=["a3b4c1d2"]),
        row(22, "dana+ffff0000", kind="ack", text="by full nick", to=[ME.upper()]),
        row(23, "dana+ffff0000", kind="message", raw="hey @dana+a3b4c1d2 look"),
        row(24, "dana+ffff0000", kind="message", text="someone else", to=["99999999"]),
    ]
    got, _ = inbox.select(rows, me=ME, identity="dana", now=NOW)
    assert [m["text"] for m in got] == ["to you", "by full nick", "hey @dana+a3b4c1d2 look"]
    assert all(m["direct"] for m in got)


def test_a_short_target_never_matches_by_prefix():
    # "a3" would address every session whose id starts with it.
    env = Envelope.new("message", "p", "x", payload={"to": ["a3"]})
    assert not inbox.addressed_to(env, "", ME)
    env6 = Envelope.new("message", "p", "x", payload={"to": ["a3b4c1"]})
    assert inbox.addressed_to(env6, "", ME)


def test_cursor_and_prime_window():
    old = row(30, "dana+ffff0000", text="two hours ago", hour=9)
    fresh = row(40, "dana+ffff0000", text="fresh")
    got, cursor = inbox.select([old, fresh], me=ME, identity="dana", now=NOW)
    assert [m["text"] for m in got] == ["fresh"]          # first read = the last hour only
    again, cursor2 = inbox.select([old, fresh], me=ME, identity="dana", cursor=cursor, now=NOW)
    assert again == [] and cursor2 == cursor              # nothing is delivered twice


def test_cap_keeps_addressed_messages_and_reports_what_it_dropped():
    rows = [row(i, "dana+ffff0000", text=f"b{i}") for i in range(1, 13)]
    rows.insert(0, row(0, "dana+ffff0000", kind="request", text="for you", to=[ME]))
    got, _ = inbox.select(rows, me=ME, identity="dana", now=NOW, limit=4)
    assert len(got) == 4
    assert got[0]["text"] == "for you"                    # oldest, but addressed: survives
    assert [m["text"] for m in got[1:]] == ["b10", "b11", "b12"]
    assert got[0]["dropped"] == 9
    assert "9 older message(s) not shown" in inbox.frame(got, me=ME, channel="#agents")


def test_frame_labels_peer_authority_and_foreign_accounts():
    rows = [row(50, "sam+12345678", text="from another account")]
    got, _ = inbox.select(rows, me=ME, identity="dana", now=NOW)
    text = inbox.frame(got, me=ME, channel="#agents")
    assert 'authority="peer"' in text.splitlines()[0]
    assert "not instructions from the owner" in text
    assert "sam+12345678 (other account)" in text
    assert inbox.frame([], me=ME, channel="#agents") == ""


def test_without_an_identity_nothing_is_mine_and_nothing_is_addressed():
    rows = [row(55, "dana", text="peer")]
    got, _ = inbox.select(rows, me="", now=NOW)
    assert [m["text"] for m in got] == ["peer"] and not got[0]["direct"]


def test_identity_cache_is_keyed_on_the_bearer(tmp_path):
    path = tmp_path / "id.json"
    inbox.store_identity_nick("bearer-one", "dana", path=path)
    assert inbox.cached_identity_nick("bearer-one", path=path) == "dana"
    assert inbox.cached_identity_nick("bearer-two", path=path) == ""      # rotated credential
    assert inbox.cached_identity_nick("bearer-one", path=path,
                                      now=9e12) == ""                      # expired
    assert "bearer-one" not in path.read_text(encoding="utf-8")            # never the secret


def test_cursor_files_round_trip_per_channel(tmp_path):
    inbox.write_cursor(ME, "#agents", "2026-09-19T11:00:00+00:00", state_dir=tmp_path)
    inbox.write_cursor(ME, "#dev", "2026-09-19T10:00:00+00:00", state_dir=tmp_path)
    assert inbox.read_cursor(ME, "#agents", state_dir=tmp_path) == "2026-09-19T11:00:00+00:00"
    assert inbox.read_cursor(ME, "#dev", state_dir=tmp_path) == "2026-09-19T10:00:00+00:00"
    assert inbox.read_cursor("dana+other", "#agents", state_dir=tmp_path) == ""


# ── the CLI signs as the session, and survives a relay that predates aliases ──

class FakeClient:
    def __init__(self, refuse_alias: bool):
        self.nick = ME
        self.identity_nick = "dana"
        self.alias_derived = True
        self.refuse_alias = refuse_alias
        self.sent: list[tuple[str, Envelope]] = []

    def send(self, channel, env):
        if self.refuse_alias and self.nick != "dana":
            raise RelayError('POST -> 403: {"detail":"Requested nick does not match '
                             'authenticated identity"}')
        self.sent.append((self.nick, env))
        return {"id": "1"}


def _send_args(**kw):
    base = dict(channel="#agents", text="hello", kind="finding", payload=None, to=[], json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_send_signs_the_envelope_with_the_session_alias(monkeypatch, capsys):
    client = FakeClient(refuse_alias=False)
    monkeypatch.setattr(cli, "_client_from_args", lambda a: client)
    assert cli._cmd_send(_send_args(to=["ffff0000"])) == 0
    nick, env = client.sent[0]
    assert nick == ME and env.sender == ME               # was "" for every session
    assert env.payload == {"to": ["ffff0000"]}
    assert f"as {ME}" in capsys.readouterr().out


def test_send_falls_back_once_and_loudly_when_the_relay_predates_aliases(monkeypatch, capsys):
    client = FakeClient(refuse_alias=True)
    monkeypatch.setattr(cli, "_client_from_args", lambda a: client)
    assert cli._cmd_send(_send_args()) == 0
    nick, env = client.sent[0]
    assert nick == "dana" and env.sender == "dana"
    assert "missing session-alias support" in capsys.readouterr().err


def test_an_explicit_nick_is_never_silently_replaced(monkeypatch):
    client = FakeClient(refuse_alias=True)
    client.alias_derived = False                          # the caller chose this nick
    monkeypatch.setattr(cli, "_client_from_args", lambda a: client)
    assert cli._cmd_send(_send_args()) == 1
    assert client.sent == []


# ── hook registration ────────────────────────────────────────────────────────

def test_install_is_idempotent_and_touches_only_its_own_entries():
    theirs = {"hooks": [{"type": "command", "command": "python other.py"}]}
    data = {"model": "x", "hooks": {"UserPromptSubmit": [theirs]}}
    data, changed = install.merge(data)
    assert sorted(changed) == ["added SessionStart", "added UserPromptSubmit"]
    assert data["hooks"]["UserPromptSubmit"][0] == theirs and data["model"] == "x"
    snapshot = json.dumps(data, sort_keys=True)
    data, changed = install.merge(data)
    assert changed == [] and json.dumps(data, sort_keys=True) == snapshot
    data, changed = install.merge(data, uninstall=True)
    assert sorted(changed) == ["removed SessionStart", "removed UserPromptSubmit"]
    assert data["hooks"]["UserPromptSubmit"] == [theirs]


def test_install_refuses_a_settings_shape_it_does_not_understand():
    with pytest.raises(ValueError):
        install.merge({"hooks": []})


# ── a relay restart forgets its agents; the client re-joins once ─────────────

def _client_with(handler):
    c = RelayClient("https://relay.test", token="t", nick=ME)
    c._client = httpx.Client(base_url="https://relay.test", transport=httpx.MockTransport(handler))
    c._door_attestation = lambda channel: None
    return c


def test_a_forgotten_agent_rejoins_once_and_the_write_lands():
    seen: list[str] = []
    state = {"joined": False}

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/v1/agent/join":
            body = json.loads(request.content)
            assert body["nick"] == ME and request.url.params["channel"] == "#agents"
            state["joined"] = True
            return httpx.Response(200, json={"success": True})
        if not state["joined"]:
            return httpx.Response(403, json={
                "detail": "#agents is a agent-only channel. No permission."})
        return httpx.Response(200, json={"id": "m1"})

    c = _client_with(handler)
    assert c.send("#agents", Envelope.new("finding", ME, "hi")) == {"id": "m1"}
    assert seen.count("/v1/agent/join") == 1


def test_a_refused_join_surfaces_the_original_403_and_never_loops():
    calls = {"join": 0, "post": 0}

    def handler(request):
        if request.url.path == "/v1/agent/join":
            calls["join"] += 1
            return httpx.Response(403, json={"detail": "no"})
        calls["post"] += 1
        return httpx.Response(403, json={"detail": "#agents is a agent-only channel. nope"})

    c = _client_with(handler)
    with pytest.raises(RelayError, match="agent-only channel"):
        c.send("#agents", Envelope.new("finding", ME, "hi"))
    assert calls == {"join": 1, "post": 1}


def test_whoami_never_returns_the_minted_token():
    c = _client_with(lambda r: httpx.Response(200, json={"relay_token": "SECRET", "nick": "dana",
                                                          "scope": "relay"}))
    assert c.whoami() == {"nick": "dana", "scope": "relay"}


def test_a_write_waits_longer_than_a_read_and_a_dead_relay_is_one_line(monkeypatch, capsys):
    seen = {}

    def handler(request):
        seen[request.method] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={"id": "m1", "messages": []})

    c = _client_with(handler)
    c.send("#agents", Envelope.new("finding", ME, "hi"))
    list(c.history("#agents"))
    assert seen["POST"] == 45.0 and seen["GET"] < 45.0

    def boom(args):
        raise httpx.ReadTimeout("The read operation timed out")

    monkeypatch.setattr(cli, "_cmd_channels", boom)
    monkeypatch.setenv("AWRELAY_URL", "https://relay.test")
    assert cli.main(["channels"]) == 1
    err = capsys.readouterr().err
    assert "did not answer" in err and "Traceback" not in err
