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


def test_the_queue_is_capped_at_max_rows_oldest_archived(tmp_path, monkeypatch):
    """Measured 2026-10-14: a bare-nick queue held 1156 rows; MAX_ROWS was declared, never read."""
    monkeypatch.setattr(outbox, "MAX_ROWS", 5)
    for i in range(8):
        assert outbox.enqueue(ME, "#agents", _env(f"m{i}"), outbox_dir=tmp_path)
    rows = outbox.pending(ME, outbox_dir=tmp_path)
    assert [r["text"] for r in rows] == ["m3", "m4", "m5", "m6", "m7"]
    arch = (tmp_path / "dana+a3b4c1d2.archived.jsonl").read_text(encoding="utf-8")
    assert arch.count('"overflow"') == 3 and '"m0"' in arch


def test_flush_caps_an_already_oversized_queue(tmp_path, monkeypatch):
    for i in range(8):
        outbox.enqueue(ME, "#agents", _env(f"m{i}"), outbox_dir=tmp_path)
    monkeypatch.setattr(outbox, "MAX_ROWS", 3)
    sent: list[str] = []
    sent_n, remaining, archived = outbox.flush(
        ME, lambda ch, env, agent: sent.append(env.text), outbox_dir=tmp_path)
    assert (sent_n, remaining, archived) == (3, 0, 5)
    assert sent == ["m5", "m6", "m7"]


def test_stale_rows_age_out_even_while_the_relay_is_down(tmp_path):
    """A halted flush used to keep EVERY later row, stale or not -- so a relay that stayed
    down meant the queue could only grow."""
    outbox.enqueue(ME, "#agents", _env("fresh-first"), outbox_dir=tmp_path, now=10_000.0)
    outbox.enqueue(ME, "#agents", _env("ancient"), outbox_dir=tmp_path, now=1000.0)
    outbox.enqueue(ME, "#agents", _env("fresh"), outbox_dir=tmp_path, now=10_000.0)

    def send(ch, env, agent):
        raise httpx.ConnectError("down")

    sent_n, remaining, archived = outbox.flush(
        ME, send, outbox_dir=tmp_path, now=1000.0 + outbox.MAX_AGE_S + 1,
        is_transport_error=lambda e: isinstance(e, httpx.TransportError))
    assert (sent_n, remaining, archived) == (0, 2, 1)
    assert [r["text"] for r in outbox.pending(ME, outbox_dir=tmp_path)] == \
        ["fresh-first", "fresh"]


def test_the_bare_identity_queue_is_drained_by_a_session_read(tmp_path, monkeypatch):
    """Host scripts (watchdog pages) send as the bare identity nick; every reader is a
    session alias. The session's inbox read must drain that queue too, posting AS the
    bare nick, and restore its own nick afterwards."""
    import awrelay.cli as cli

    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    outbox.enqueue("dana", "#agents", Envelope.new("alert", "dana", "gateway down"),
                   outbox_dir=tmp_path)
    outbox.enqueue(ME, "#agents", _env("mine"), outbox_dir=tmp_path)
    sent: list[tuple[str, str]] = []

    class _Client:
        nick = ME
        identity_nick = "dana"
        alias_derived = True

        def send(self, ch, env, agent=True):
            sent.append((self.nick, env.text))

    c = _Client()
    cli._flush_outbox(c, ME, "")
    assert sent == [(ME, "mine"), ("dana", "gateway down")]
    assert c.nick == ME and c.alias_derived is True, "the session nick is restored"
    assert outbox.pending("dana", outbox_dir=tmp_path) == []


def test_sweep_ages_out_every_other_queue_without_sending(tmp_path):
    """Ended sessions and agent nicks never read again, so their queues never flushed:
    measured 37 files, 36 holding rows days old. The sweep archives stale rows in every
    queue, leaves fresh rows for their owner, and skips the queues the caller flushes."""
    old, fresh = 1000.0, 1000.0 + outbox.MAX_AGE_S + 10
    outbox.enqueue("dana+dead0001", "#agents", _env("ancient"), outbox_dir=tmp_path, now=old)
    outbox.enqueue("dana+live0002", "#agents", _env("recent"), outbox_dir=tmp_path,
                   now=fresh - 5)
    outbox.enqueue(ME, "#agents", _env("mine-old"), outbox_dir=tmp_path, now=old)
    changed, archived = outbox.sweep(outbox_dir=tmp_path, now=fresh, skip=(ME,))
    assert (changed, archived) == (1, 1)
    assert outbox.pending("dana+dead0001", outbox_dir=tmp_path) == []
    assert [r["text"] for r in outbox.pending("dana+live0002", outbox_dir=tmp_path)] == \
        ["recent"], "a live peer's fresh row is theirs to send"
    assert [r["text"] for r in outbox.pending(ME, outbox_dir=tmp_path)] == ["mine-old"], \
        "a skipped queue is left to the caller's own flush"
    arch = (tmp_path / "dana+dead0001.archived.jsonl").read_text(encoding="utf-8")
    assert "ancient" in arch and '"stale"' in arch


def test_sweep_is_throttled(tmp_path):
    outbox.enqueue("x", "#agents", _env("a"), outbox_dir=tmp_path, now=1.0)
    assert outbox.sweep(outbox_dir=tmp_path, min_interval_s=600) == (1, 1)
    outbox.enqueue("y", "#agents", _env("b"), outbox_dir=tmp_path, now=1.0)
    assert outbox.sweep(outbox_dir=tmp_path, min_interval_s=600) == (0, 0), \
        "a second sweep inside the interval does nothing"


def test_two_concurrent_flushers_send_each_row_once_and_keep_a_mid_flush_append(tmp_path):
    """Review of #12866: every live session of an identity flushes the bare queue, and
    host scripts append to it while the relay flaps. Without a claim each flusher sent
    every row, and a row appended between a flush's read and its rewrite was lost."""
    import threading

    bare = "dana"
    for i in range(20):
        outbox.enqueue(bare, "#agents", _env(f"page{i}"), outbox_dir=tmp_path)
    sent: list[str] = []
    second: list[tuple[int, int, int]] = []
    first: list[tuple[int, int, int]] = []
    other_flusher_done = threading.Event()

    def other_session():
        # A second session's inbox hook and a host script arrive while flusher 1 is
        # mid-send (blocked on the network).
        second.append(outbox.flush(bare, lambda c, e, a: sent.append(e.text),
                                   outbox_dir=tmp_path))
        outbox.enqueue(bare, "#agents", _env("appended-mid-flush"), outbox_dir=tmp_path)
        other_flusher_done.set()

    def slow_send(ch, env, agent):
        sent.append(env.text)
        if env.text == "page0":
            threading.Thread(target=other_session).start()
            assert other_flusher_done.wait(timeout=20)

    t = threading.Thread(target=lambda: first.append(
        outbox.flush(bare, slow_send, outbox_dir=tmp_path)))
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    assert second == [(0, 0, 0)], "a second flusher must leave a live claim alone"
    assert first == [(20, 0, 0)]
    assert sent == [f"page{i}" for i in range(20)], "each row sent exactly once, in order"
    assert [r["text"] for r in outbox.pending(bare, outbox_dir=tmp_path)] == \
        ["appended-mid-flush"], "a row appended mid-flush survives for the next flush"
    assert not (tmp_path / "dana.jsonl.flushing").exists()
    assert not (tmp_path / "dana.jsonl.lock").exists()


def test_a_halted_flush_puts_kept_rows_in_front_of_mid_flush_appends(tmp_path):
    for t in ("a", "b"):
        outbox.enqueue(ME, "#agents", _env(t), outbox_dir=tmp_path)

    def send(ch, env, agent):
        outbox.enqueue(ME, "#agents", _env("late"), outbox_dir=tmp_path)
        raise httpx.ConnectError("down")

    result = outbox.flush(ME, send, outbox_dir=tmp_path,
                          is_transport_error=lambda e: isinstance(e, httpx.TransportError))
    assert result == (0, 2, 0)
    assert [r["text"] for r in outbox.pending(ME, outbox_dir=tmp_path)] == ["a", "b", "late"]


def test_a_dead_flushers_claim_is_adopted_once_stale(tmp_path):
    import json
    import os

    claim = tmp_path / "dana.jsonl.flushing"
    claim.write_text(json.dumps({"queued_at": 5000.0, "channel": "#agents",
                                 "kind": "finding", "sender": "dana",
                                 "text": "orphan"}) + "\n", encoding="utf-8")
    outbox.enqueue("dana", "#agents", _env("newer"), outbox_dir=tmp_path, now=5000.0)
    sent: list[str] = []
    # Fresh claim: a live flusher owns it -- hands off.
    assert outbox.flush("dana", lambda c, e, a: sent.append(e.text), outbox_dir=tmp_path,
                        now=5001.0) == (0, 0, 0)
    assert [r["text"] for r in outbox.pending("dana", outbox_dir=tmp_path)] == \
        ["orphan", "newer"], "pending counts claimed rows too"
    old = claim.stat().st_mtime - outbox.LOCK_STALE_S - 5
    os.utime(claim, (old, old))
    assert outbox.flush("dana", lambda c, e, a: sent.append(e.text), outbox_dir=tmp_path,
                        now=5001.0) == (2, 0, 0)
    assert sent == ["orphan", "newer"], "the dead flusher's rows go first"
    assert not claim.exists()


def test_a_held_lock_makes_sweep_skip_and_a_stale_lock_is_broken(tmp_path):
    import os

    outbox.enqueue("x", "#agents", _env("a"), outbox_dir=tmp_path, now=1.0)
    lock = tmp_path / "x.jsonl.lock"
    lock.write_text("999999", encoding="utf-8")
    assert outbox.sweep(outbox_dir=tmp_path, now=10.0 ** 6) == (0, 0), \
        "a queue whose lock is held is skipped, not rewritten under its owner"
    old = lock.stat().st_mtime - outbox.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert outbox.sweep(outbox_dir=tmp_path, now=10.0 ** 6) == (1, 1)
    assert not lock.exists()


def test_a_stale_orphan_claim_is_swept_even_when_its_nick_never_reads(tmp_path):
    import os

    outbox.enqueue("gone", "#agents", _env("old"), outbox_dir=tmp_path, now=1.0)
    q = tmp_path / "gone.jsonl"
    claim = tmp_path / "gone.jsonl.flushing"
    os.replace(q, claim)  # the flusher died after claiming
    old = claim.stat().st_mtime - outbox.LOCK_STALE_S - 5
    os.utime(claim, (old, old))
    assert outbox.sweep(outbox_dir=tmp_path, now=10.0 ** 6) == (1, 1)
    assert not claim.exists() and not q.exists()


def _send_into(sent: list[str]):
    return lambda c, e, a: sent.append(e.text)


def _age(path, by: float) -> None:
    import os

    old = path.stat().st_mtime - by
    os.utime(path, (old, old))


def test_a_queue_held_open_by_a_reader_is_still_sent_exactly_once(tmp_path):
    """Review of #12866: on Windows `path.unlink()` of a queue another process holds open
    (every session's inbox hook calls `pending()` at each prompt) raised PermissionError
    AFTER the claim was written -- claim and queue both held the rows, the flush returned
    (0, 0, 0), and once the claim went stale every row was sent twice."""
    for t in ("m0", "m1", "m2"):
        outbox.enqueue(ME, "#agents", _env(t), outbox_dir=tmp_path)
    sent: list[str] = []
    queue = outbox._path(ME, tmp_path)
    with open(queue, encoding="utf-8"):
        outbox.flush(ME, _send_into(sent), outbox_dir=tmp_path)
    claim = outbox._claim_path(queue)
    if claim.exists():
        _age(claim, outbox.LOCK_STALE_S + 5)
    outbox.flush(ME, _send_into(sent), outbox_dir=tmp_path)
    assert sent == ["m0", "m1", "m2"]
    assert outbox.pending(ME, outbox_dir=tmp_path) == []


def test_an_undeletable_queue_is_truncated_never_duplicated(tmp_path, monkeypatch):
    """The Windows sharing violation, emulated on every OS: unlink and replace of the
    queue and its claim refuse with PermissionError."""
    import os
    from pathlib import Path

    for t in ("m0", "m1", "m2"):
        outbox.enqueue(ME, "#agents", _env(t), outbox_dir=tmp_path)
    queue = outbox._path(ME, tmp_path)
    shared = {queue.name, outbox._claim_path(queue).name}
    real_unlink, real_replace = Path.unlink, os.replace

    def unlink(self, missing_ok=False):
        if self.name in shared:
            raise PermissionError(32, "in use", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    def replace(src, dst):
        if Path(dst).name in shared and Path(dst).exists():
            raise PermissionError(32, "in use", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(outbox, "SHARE_RETRY_S", 0.0)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(os, "replace", replace)
    sent: list[str] = []
    assert outbox.flush(ME, _send_into(sent), outbox_dir=tmp_path) == (3, 0, 0)
    monkeypatch.undo()
    claim = outbox._claim_path(queue)
    if claim.exists():
        _age(claim, outbox.LOCK_STALE_S + 5)
    assert outbox.flush(ME, _send_into(sent), outbox_dir=tmp_path) == (0, 0, 0)
    assert sent == ["m0", "m1", "m2"]


def test_a_claim_whose_queue_cannot_be_emptied_is_rolled_back(tmp_path, monkeypatch):
    """When the queue cannot be emptied at all, the claim is undone under the lock: the
    rows live ONLY in the queue, an adopted dead claim keeps its rows and its staleness,
    and the next flush sends each row once."""
    import json

    queue = outbox._path("dana", tmp_path)
    claim = outbox._claim_path(queue)
    claim.write_text(json.dumps({"queued_at": 5000.0, "channel": "#agents",
                                 "kind": "finding", "sender": "dana",
                                 "text": "orphan"}) + "\n", encoding="utf-8")
    _age(claim, outbox.LOCK_STALE_S + 5)
    outbox.enqueue("dana", "#agents", _env("newer"), outbox_dir=tmp_path, now=5000.0)
    real_clear = outbox._clear
    monkeypatch.setattr(outbox, "_clear", lambda p, keep_file=True: False if p == queue
                        else real_clear(p, keep_file=keep_file))
    sent: list[str] = []
    assert outbox.flush("dana", _send_into(sent), outbox_dir=tmp_path,
                        now=5001.0) == (0, 0, 0)
    assert sent == []
    assert [r["text"] for r in outbox._read_rows(claim)] == ["orphan"], \
        "the rollback restores the adopted rows only"
    assert [r["text"] for r in outbox._read_rows(queue)] == ["newer"]
    monkeypatch.undo()
    assert outbox.flush("dana", _send_into(sent), outbox_dir=tmp_path,
                        now=5001.0) == (2, 0, 0), "the rolled-back claim is still stale"
    assert sent == ["orphan", "newer"]


def test_a_fresh_claim_whose_queue_cannot_be_emptied_is_removed(tmp_path, monkeypatch):
    for t in ("m0", "m1"):
        outbox.enqueue(ME, "#agents", _env(t), outbox_dir=tmp_path)
    queue = outbox._path(ME, tmp_path)
    real_clear = outbox._clear
    monkeypatch.setattr(outbox, "_clear", lambda p, keep_file=True: False if p == queue
                        else real_clear(p, keep_file=keep_file))
    sent: list[str] = []
    assert outbox.flush(ME, _send_into(sent), outbox_dir=tmp_path) == (0, 0, 0)
    assert not outbox._claim_path(queue).exists(), "no claim left to block or duplicate"
    monkeypatch.undo()
    assert outbox.flush(ME, _send_into(sent), outbox_dir=tmp_path) == (2, 0, 0)
    assert sent == ["m0", "m1"]
