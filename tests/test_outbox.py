"""A send the relay could not take is kept, ordered, flushed once, never lost.

Measured 2026-09-20: the relay went down with the fleet VM for four hours and
every `awrelay send` in that window exited 1 with its text gone.
"""
from __future__ import annotations

import httpx
import pytest

from awrelay import outbox
from awrelay.envelope import Envelope

ME = "dana+a3b4c1d2"


def _env(text: str, corr: str | None = None) -> Envelope:
    e = Envelope.new("finding", ME, text, payload={"to": ["lyra+1111aaaa"]})
    if corr:
        e.correlation_id = corr
    return e


def test_a_transport_failure_is_queued_in_order(tmp_path):
    assert outbox.enqueue(ME, "#agents", _env("first"), outbox_dir=tmp_path)
    assert outbox.enqueue(ME, "#agents", _env("second"), outbox_dir=tmp_path)
    rows = outbox.pending(ME, outbox_dir=tmp_path)
    assert [r["text"] for r in rows] == ["first", "second"]
    assert rows[0]["payload"] == {"to": ["lyra+1111aaaa"]}, "addressing survives the queue"


def test_flush_sends_in_order_and_empties_the_queue(tmp_path):
    outbox.enqueue(ME, "#agents", _env("first", corr="c-1"), outbox_dir=tmp_path)
    outbox.enqueue(ME, "#ops", _env("second"), outbox_dir=tmp_path)
    sent: list[tuple[str, str, str | None]] = []
    sent_n, remaining, archived = outbox.flush(
        ME, lambda ch, env, agent: sent.append((ch, env.text, env.correlation_id)),
        outbox_dir=tmp_path)
    assert (sent_n, remaining, archived) == (2, 0, 0)
    assert sent == [("#agents", "first", "c-1"), ("#ops", "second", None)], \
        "order and the original correlation id must survive the outage"
    assert outbox.pending(ME, outbox_dir=tmp_path) == []


def test_flush_halts_at_the_first_transport_failure_and_keeps_order(tmp_path):
    for t in ("a", "b", "c"):
        outbox.enqueue(ME, "#agents", _env(t), outbox_dir=tmp_path)
    calls: list[str] = []

    def send(ch, env, agent):
        calls.append(env.text)
        if env.text == "b":
            raise httpx.ConnectError("still down")

    sent_n, remaining, archived = outbox.flush(
        ME, send, outbox_dir=tmp_path,
        is_transport_error=lambda e: isinstance(e, httpx.TransportError))
    assert (sent_n, remaining, archived) == (1, 2, 0)
    assert calls == ["a", "b"], "c must NOT be tried ahead of b"
    assert [r["text"] for r in outbox.pending(ME, outbox_dir=tmp_path)] == ["b", "c"]


def test_a_refusal_is_archived_not_retried(tmp_path):
    outbox.enqueue(ME, "#agents", _env("refused"), outbox_dir=tmp_path)
    outbox.enqueue(ME, "#agents", _env("fine"), outbox_dir=tmp_path)

    def send(ch, env, agent):
        if env.text == "refused":
            raise RuntimeError("403: behind a door")

    sent_n, remaining, archived = outbox.flush(
        ME, send, outbox_dir=tmp_path,
        is_transport_error=lambda e: isinstance(e, httpx.TransportError))
    assert (sent_n, remaining, archived) == (1, 0, 1)
    arch = (tmp_path / "dana+a3b4c1d2.archived.jsonl").read_text(encoding="utf-8")
    assert "refused" in arch and "403" in arch, "a refusal is kept as a record, never replayed"


def test_a_stale_row_is_archived_never_replayed(tmp_path):
    outbox.enqueue(ME, "#agents", _env("ancient"), outbox_dir=tmp_path, now=1000.0)
    sent: list[str] = []
    sent_n, remaining, archived = outbox.flush(
        ME, lambda ch, env, agent: sent.append(env.text), outbox_dir=tmp_path,
        now=1000.0 + outbox.MAX_AGE_S + 1)
    assert (sent_n, remaining, archived) == (0, 0, 1) and sent == []


@pytest.mark.parametrize("bad", ["not json", '{"channel": "#x"}', ""])
def test_a_corrupt_line_never_stalls_the_queue(tmp_path, bad):
    path = tmp_path / "dana+a3b4c1d2.jsonl"
    path.write_text(bad + "\n", encoding="utf-8")
    outbox.enqueue(ME, "#agents", _env("good"), outbox_dir=tmp_path)
    sent: list[str] = []
    sent_n, remaining, _ = outbox.flush(ME, lambda ch, env, agent: sent.append(env.text),
                                        outbox_dir=tmp_path)
    assert sent == ["good"] and remaining == 0


def test_a_row_queued_before_the_nick_was_known_is_not_stranded(tmp_path, monkeypatch):
    """The first send after a cold start during an outage has no nick: the relay is
    what answers whoami. It queues under the empty key, and a flush keyed on the
    resolved nick must still find it -- otherwise the one message a peer is waiting
    on is the one that never lands."""
    import awrelay.cli as cli

    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    outbox.enqueue("", "#agents", Envelope.new("finding", "", "orphaned",
                                               payload={"to": ["lyra+1111aaaa"]}),
                   outbox_dir=tmp_path)
    outbox.enqueue(ME, "#agents", _env("mine"), outbox_dir=tmp_path)
    sent: list[tuple[str, str]] = []

    class _Client:
        def send(self, ch, env, agent=True):
            sent.append((env.sender, env.text))

    cli._flush_outbox(_Client(), ME, "")
    assert sent == [(ME, "mine"), (ME, "orphaned")], \
        "both queues drain, and the orphan is signed with the nick we now know"
    assert outbox.pending("", outbox_dir=tmp_path) == []
    assert outbox.pending(ME, outbox_dir=tmp_path) == []
