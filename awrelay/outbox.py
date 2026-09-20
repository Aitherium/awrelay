"""outbox -- a send the relay could not take is kept, not lost.

Measured 2026-09-20: the relay lives on the fleet VM and went down with it
from 03:28 to 07:31. Every `awrelay send` in that window exited 1 and its text
was gone -- from a session that could not know whether the peer it was warning
would ever hear it. A bus that drops writes during its own outage is not a bus
sessions can coordinate on.

So a TRANSPORT failure (nothing answered) queues the envelope here, on disk,
per sending nick, in order. A REFUSAL (403, a door, a bad channel) does not:
the relay answered, and a refused write must not be retried behind the owner's
back. The queue is flushed at the head of every inbox read -- the same hooks
that fire at each prompt and each tool call -- so a queued line lands within
seconds of the relay returning, before the session reads anything new.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from awrelay.envelope import Envelope

OUTBOX_DIR = Path.home() / ".aither" / "relay-outbox"
#: A queue older than this is stale enough that replaying it would mislead
#: more than it informs; it is archived, never silently dropped.
MAX_AGE_S = 6 * 3600
MAX_ROWS = 200


def _path(me: str, outbox_dir: Optional[Path] = None) -> Path:
    # Resolved at CALL time, never bound as a default: a default freezes the
    # module-level path at import, so a test (or a run with a redirected home)
    # that repoints OUTBOX_DIR still reads and writes the real queue.
    outbox_dir = outbox_dir or OUTBOX_DIR
    safe = "".join(c if c.isalnum() or c in "+-_" else "_" for c in (me or "anonymous"))
    return outbox_dir / (safe + ".jsonl")


def enqueue(me: str, channel: str, envelope: Envelope, *, agent: bool = True,
            outbox_dir: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """Append one send to the queue. False when the disk refused -- the caller must say
    so, because the symptom is a message that was neither sent nor kept."""
    row = {
        "queued_at": now or time.time(), "channel": channel, "agent": agent,
        "kind": envelope.kind.value, "sender": envelope.sender, "text": envelope.text,
        "payload": envelope.payload or {}, "correlation_id": envelope.correlation_id,
        "sent_at": envelope.sent_at,
    }
    path = _path(me, outbox_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def pending(me: str, *, outbox_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    path = _path(me, outbox_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("channel") and row.get("text") is not None:
            rows.append(row)
    return rows


def _rebuild(row: dict[str, Any]) -> Envelope:
    env = Envelope.new(row["kind"], row.get("sender") or "", row["text"],
                       payload=row.get("payload") or {})
    # Keep the ORIGINAL stamps: a reader must see when it was said, not when the
    # relay came back, and an ACK must still pair with the request it answers.
    if row.get("correlation_id"):
        env.correlation_id = row["correlation_id"]
    if row.get("sent_at"):
        env.sent_at = row["sent_at"]
    return env


def flush(me: str, send: Callable[[str, Envelope, bool], Any], *,
          outbox_dir: Optional[Path] = None, now: Optional[float] = None,
          is_transport_error: Callable[[BaseException], bool] = lambda e: True,
          ) -> tuple[int, int, int]:
    """Send every queued row in order. (sent, remaining, archived).

    Stops at the FIRST transport failure so order is kept -- a later row landing
    before an earlier one is worse than both waiting. A row the relay REFUSES is
    dropped from the queue with a note (retrying a refusal is not this module's
    call). Rows older than MAX_AGE_S are archived, never replayed.
    """
    now = now or time.time()
    rows = pending(me, outbox_dir=outbox_dir)
    if not rows:
        return 0, 0, 0
    path = _path(me, outbox_dir)
    keep: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    sent = 0
    halted = False
    for row in rows:
        if halted:
            keep.append(row)
            continue
        if now - float(row.get("queued_at") or now) > MAX_AGE_S:
            archived.append(row)
            continue
        try:
            send(row["channel"], _rebuild(row), bool(row.get("agent", True)))
            sent += 1
        except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
            if is_transport_error(exc):
                keep.append(row)
                halted = True
            else:
                archived.append(dict(row, refused=f"{type(exc).__name__}: {exc}"[:200]))
    _rewrite(path, keep)
    if archived:
        _archive(path, archived)
    return sent, len(keep), len(archived)


def _rewrite(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        if not rows:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return  # next flush retries; a row is never lost by a failed rewrite


def _archive(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        arch = path.with_name(path.stem + ".archived.jsonl")
        with open(arch, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        return
