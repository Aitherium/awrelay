"""A first-party session must not post to the relay as an anonymous walk-in.

Measured 2026-09-19 08:50, announcing a maintenance window from a Claude Code session: every
channel refused -- "Pick a nick to post" on the open ones, "Verify identity" on the member
ones. Nothing had set AWRELAY_TOKEN or AWRELAY_NICK, so the client sent no bearer and no nick,
and the relay correctly saw an anonymous caller. The `applies_to` door fix that exempts
members and fleet services from the #agents knock cannot help a caller that never says who it
is. The same bearer the MCP stdio bridge reads on every reconnect sits at
~/.aither/session-bearer; the CLI now falls back to it.
"""
from __future__ import annotations

import argparse

import pytest

from awrelay import cli


def _args(**kw):
    base = {"url": "http://relay.test", "token": None, "nick": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_session_bearer_is_used_when_nothing_else_is_set(tmp_path, monkeypatch):
    monkeypatch.delenv("AWRELAY_TOKEN", raising=False)
    monkeypatch.delenv("AWRELAY_NICK", raising=False)
    bearer = tmp_path / "session-bearer"
    bearer.write_text("tok-from-session\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_BEARER_FILE", str(bearer))
    client = cli._client_from_args(_args())
    assert client._token == "tok-from-session", (
        "with no explicit token the client must present the session bearer, or the relay "
        "sees an anonymous walk-in and refuses every member channel")


def test_with_a_bearer_no_nick_is_invented(tmp_path, monkeypatch):
    """The relay binds the nick to the authenticated identity and answers 403 'Requested nick
    does not match authenticated identity' to any other -- measured on the first attempt."""
    monkeypatch.delenv("AWRELAY_TOKEN", raising=False)
    monkeypatch.delenv("AWRELAY_NICK", raising=False)
    bearer = tmp_path / "session-bearer"
    bearer.write_text("tok", encoding="utf-8")
    monkeypatch.setattr(cli, "_BEARER_FILE", str(bearer))
    assert cli._client_from_args(_args()).nick is None


def test_without_any_identity_the_cli_refuses_and_names_the_mint_command(tmp_path, monkeypatch, capsys):
    """NO anonymous path. A first-party session either presents its identity or does not post;
    degrading to a walk-in with an invented nick is the silent fallback the owner forbids."""
    monkeypatch.delenv("AWRELAY_TOKEN", raising=False)
    monkeypatch.delenv("AWRELAY_NICK", raising=False)
    monkeypatch.setattr(cli, "_BEARER_FILE", str(tmp_path / "absent"))
    with pytest.raises(SystemExit) as exc:
        cli._client_from_args(_args())
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "mint_session_bearer.py" in err, "the refusal must name the remedy"
    assert "no identity" in err


def test_explicit_token_and_nick_still_win(tmp_path, monkeypatch):
    bearer = tmp_path / "session-bearer"
    bearer.write_text("session-tok", encoding="utf-8")
    monkeypatch.setattr(cli, "_BEARER_FILE", str(bearer))
    client = cli._client_from_args(_args(token="explicit", nick="me"))
    assert client._token == "explicit" and client.nick == "me"
