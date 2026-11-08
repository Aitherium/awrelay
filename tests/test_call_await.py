"""`awrelay call --await` / `awrelay reply`: request/response over the existing envelope.

A fake transport stands in for the relay: it records every envelope sent and answers
history polls from a script, so these tests prove the wire contract (a REQUEST with a
fresh correlation_id; a reply matched ONLY by that id) and the exit codes, with a
fake clock so a 30-second wait costs nothing.
"""
from __future__ import annotations  # noqa: I001 - awrelay is first-party here, third-party to --isolated ruff

import json

import httpx
import pytest

from awrelay import cli, inbox
from awrelay.client import RelayError
from awrelay.envelope import Envelope, Kind


class FakeClient:
    """send() records; history() replays the sent log plus scripted peer replies."""

    def __init__(self, nick="caller"):
        self.nick = nick
        self.identity_nick = nick
        self.alias_derived = False
        self.sent: list[tuple[str, Envelope]] = []
        self.polls = 0
        self.on_poll = None          # fn(poll_number, request_envelope) -> list[Envelope]
        self.history_error = None    # exception raised by every history() call
        self.send_error = None

    def send(self, channel, envelope, *, agent=True):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((channel, envelope))
        return {"id": f"m{len(self.sent)}"}

    def history(self, channel, *, limit=50, envelopes_only=False):
        self.polls += 1
        if self.history_error is not None:
            raise self.history_error
        rows = [env for ch, env in self.sent if ch == channel]
        if self.on_poll is not None:
            rows += self.on_poll(self.polls, self.sent[0][1])
        return iter(rows)


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    clock = {"t": 1000.0}
    monkeypatch.setattr(cli, "_client_from_args", lambda args: client)
    monkeypatch.setattr(cli, "_now", lambda: clock["t"])

    def sleep(s):
        clock["t"] += s

    monkeypatch.setattr(cli, "_sleep", sleep)
    client.clock = clock
    return client


def _ack(corr, text, sender="lyra"):
    return Envelope.new(Kind.ACK, sender, text, correlation_id=corr)


def test_reply_arrives_exit_0_text_on_stdout(fake, capsys):
    fake.on_poll = lambda n, req: [_ack(req.correlation_id, "gate passed")] if n >= 3 else []
    rc = cli.main(["call", "#agents", "rerun the gate", "--to", "lyra", "--await", "30"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "gate passed"
    channel, req = fake.sent[0]
    assert channel == "#agents" and req.kind is Kind.REQUEST
    assert req.correlation_id and len(req.correlation_id) >= 32
    assert req.payload["to"] == ["lyra"] and req.payload["reply_channel"] == "#agents"
    assert fake.polls == 3


def test_each_call_gets_a_fresh_correlation_id(fake):
    fake.on_poll = lambda n, req: [_ack(fake.sent[-1][1].correlation_id, "ok")]
    assert cli.main(["call", "#a", "one", "--to", "x", "--await", "5"]) == 0
    assert cli.main(["call", "#a", "two", "--to", "x", "--await", "5"]) == 0
    assert fake.sent[0][1].correlation_id != fake.sent[1][1].correlation_id


def test_timeout_exit_3(fake, capsys):
    rc = cli.main(["call", "#agents", "anyone?", "--to", "lyra", "--await", "10",
                   "--interval", "2"])
    assert rc == cli.EXIT_CALL_TIMEOUT == 3
    assert "no reply" in capsys.readouterr().err
    assert fake.clock["t"] == pytest.approx(1010.0)
    assert len(fake.sent) == 1, "a call sends exactly one request"


def test_mismatched_correlation_and_own_request_are_ignored(fake, capsys):
    def peers(n, req):
        return [
            _ack("someone-elses-id", "wrong answer"),
            Envelope.new(Kind.FINDING, "lyra", "unrelated finding"),
            Envelope.new(Kind.REQUEST, "caller", "echo of my request",
                         correlation_id=req.correlation_id),
        ]
    fake.on_poll = peers
    rc = cli.main(["call", "#agents", "q", "--to", "lyra", "--await", "6", "--interval", "2"])
    assert rc == 3
    assert "wrong answer" not in capsys.readouterr().out


def test_json_output_carries_the_reply_envelope(fake, capsys):
    fake.on_poll = lambda n, req: [Envelope.new(Kind.FINDING, "lyra", "found it",
                                                payload={"line": 7},
                                                correlation_id=req.correlation_id)]
    assert cli.main(["--json", "call", "#a", "q", "--to", "lyra", "--await", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["text"] == "found it" and out["kind"] == "finding" and out["payload"] == {"line": 7}


@pytest.mark.parametrize("argv", [
    ["call", "#a", "q", "--to", "lyra", "--await", "0"],
    ["call", "#a", "q", "--to", "lyra", "--await", "-5"],
    ["call", "#a", "   ", "--to", "lyra"],
    ["call", "#a", "q", "--to", " "],
    ["call", "", "q", "--to", "lyra"],
    ["call", "#a", "q", "--to", "lyra", "--payload", "[1]"],
    ["call", "#a", "q", "--to", "lyra", "--await", "5", "--interval", "9"],
])
def test_bad_args_refused_before_anything_is_sent(fake, argv):
    assert cli.main(argv) == 2
    assert fake.sent == [] and fake.polls == 0


def test_missing_to_is_an_argparse_error(fake):
    with pytest.raises(SystemExit) as exc:
        cli.main(["call", "#a", "q"])
    assert exc.value.code == 2
    assert fake.sent == []


def test_send_failure_exit_2(fake):
    fake.send_error = RelayError("POST -> 403: nope")
    assert cli.main(["call", "#a", "q", "--to", "lyra", "--await", "5"]) == 2
    fake.send_error = httpx.ConnectError("refused")
    assert cli.main(["call", "#a", "q", "--to", "lyra", "--await", "5"]) == 2


def test_relay_gone_while_waiting_exit_2(fake):
    fake.history_error = httpx.ConnectError("refused")
    rc = cli.main(["call", "#a", "q", "--to", "lyra", "--await", "60", "--interval", "1"])
    assert rc == 2
    assert fake.polls == cli.CALL_POLL_FAILURES


def test_reply_sends_an_ack_carrying_the_id(fake, capsys):
    rc = cli.main(["reply", "abc123", "done, exit 0", "--channel", "#ops", "--to", "caller"])
    assert rc == 0
    channel, env = fake.sent[0]
    assert channel == "#ops" and env.kind is Kind.ACK
    assert env.correlation_id == "abc123" and env.text == "done, exit 0"
    assert env.payload == {"to": ["caller"]}
    # ... and that ack is exactly what a waiting call matches.
    assert cli.find_reply([env], "abc123") is env


def test_reply_bad_args_and_transport(fake):
    assert cli.main(["reply", "has space", "x"]) == 2
    assert cli.main(["reply", "abc", "  "]) == 2
    assert fake.sent == []
    fake.send_error = httpx.ConnectError("refused")
    assert cli.main(["reply", "abc", "x"]) == 2


def test_inbox_frame_tells_the_peer_how_to_answer():
    req = Envelope.new(Kind.REQUEST, "caller+s1", "rerun the gate", payload={"to": ["me"]},
                       correlation_id="feedc0de")
    msgs = [{"id": "1", "timestamp": "2026-11-07T10:00:00+00:00", "nick": "caller+s1",
             "content": req.to_relay_content()}]
    rows, _ = inbox.select(msgs, me="me", cursor="2026-11-07T09:00:00+00:00")
    text = inbox.frame(rows, me="me", channel="#agents")
    assert "awrelay reply feedc0de" in text and "--channel '#agents'" in text
