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

Concurrency. One queue has many writers and, for the bare identity nick, many
readers: host scripts append while every live session of that identity flushes.
A read-then-rewrite with no claim sent each row once PER flusher and overwrote any
row appended between the read and the rewrite. So every mutation of a queue holds
`<queue>.lock` (O_CREAT|O_EXCL, broken only when older than LOCK_STALE_S) for
milliseconds of file work, never across the network: a flush CLAIMS the queue
(moves its rows into `<queue>.flushing` and empties it), sends with no lock held,
and merges the rows it kept back IN FRONT of whatever was appended meanwhile. A
second flusher finds the claim and leaves it alone; an appender is never blocked
on a send. A claim whose flusher died is adopted once it goes LOCK_STALE_S without
a heartbeat (the flusher rewrites it after every row, so at most one row is resent).

Windows refuses to delete or replace a file another process holds open, and every
session's inbox hook reads the queue (`pending()`) at each prompt. So a remove or a
replace retries a sharing violation briefly, then truncates or overwrites IN PLACE
(an open handle shares writes); and a claim whose queue still cannot be emptied is
ROLLED BACK under the lock -- claim and queue never both hold a row.
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
#: The most rows one nick's queue may hold. Measured 2026-10-14: one bare-nick queue had
#: grown to 1156 rows because nothing enforced this (it was declared, never read).
#: The OLDEST overflow is archived -- a queue is a bridge over an outage, not a log.
MAX_ROWS = 200
#: A lock or a claim untouched this long belongs to a dead process. Critical sections
#: are milliseconds; a live flusher touches its claim after every row it sends.
LOCK_STALE_S = 120.0
#: How long a mutation waits for the queue's lock before giving up (enqueue appends
#: anyway -- losing the line is worse than the narrow race the lock closes).
LOCK_WAIT_S = 5.0


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
    except OSError:
        return False
    locked = _lock(path)
    try:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return False
        if locked:
            _cap(path)
    finally:
        if locked:
            _unlock(path)
    return True


def _cap(path: Path) -> int:
    """Archive the oldest rows beyond MAX_ROWS. The caller holds the queue's lock."""
    rows = _read_rows(path)
    over = len(rows) - MAX_ROWS
    if over <= 0:
        return 0
    _archive(path, [dict(r, archived="overflow") for r in rows[:over]])
    _rewrite(path, rows[over:])
    return over


def pending(me: str, *, outbox_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every row not yet delivered: a claim's rows (in flight, or left by a dead
    flusher) first, then the queue's."""
    path = _path(me, outbox_dir)
    return _read_rows(_claim_path(path)) + _read_rows(path)


def _read_rows(path: Path) -> list[dict[str, Any]]:
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
    call). Rows older than MAX_AGE_S are archived, never replayed -- even when the
    relay is still down, so a dead relay cannot grow the queue without bound. Rows
    beyond the newest MAX_ROWS are archived as overflow before anything is sent.

    Exactly once across concurrent flushers: the queue is CLAIMED under its lock
    before anything is sent (module docstring). When another live flusher holds the
    claim this sends nothing and returns (0, 0, 0).
    """
    now = now or time.time()
    path = _path(me, outbox_dir)
    rows = _claim(path)
    if not rows:
        return 0, 0, 0
    claim = _claim_path(path)
    archived_n = 0
    over = len(rows) - MAX_ROWS
    if over > 0:
        _archive(path, [dict(r, archived="overflow") for r in rows[:over]])
        archived_n += over
        rows = rows[over:]
    keep: list[dict[str, Any]] = []
    sent = 0
    halted = False
    for i, row in enumerate(rows):
        # Staleness is judged BEFORE the halt: a relay that stays down must still
        # let old rows age out, or the queue only ever grows.
        if now - float(row.get("queued_at") or now) > MAX_AGE_S:
            _archive(path, [dict(row, archived="stale")])
            archived_n += 1
        elif halted:
            keep.append(row)
        else:
            try:
                send(row["channel"], _rebuild(row), bool(row.get("agent", True)))
                sent += 1
            except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
                if is_transport_error(exc):
                    keep.append(row)
                    halted = True
                else:
                    refused = f"{type(exc).__name__}: {exc}"[:200]
                    _archive(path, [dict(row, refused=refused)])
                    archived_n += 1
        # Progress + heartbeat: the claim holds exactly the rows not yet decided, so a
        # flusher that dies here costs at most the one row it was sending. It is
        # rewritten even when empty -- an absent claim would invite a second flusher.
        if not halted:
            _write_claim(claim, keep + rows[i + 1:])
    _release_claim(path, keep)
    return sent, len(keep), archived_n


def _claim_path(path: Path) -> Path:
    return path.with_name(path.name + ".flushing")


def _claim(path: Path) -> list[dict[str, Any]]:
    """Move the queue's rows into its claim file and empty the queue, under the lock.
    [] when there is nothing to send or a LIVE flusher already holds the claim. A claim
    left by a dead flusher (untouched for LOCK_STALE_S) is adopted, its rows first."""
    claim = _claim_path(path)
    if not path.exists() and not claim.exists():
        return []
    if not _lock(path):
        return []
    try:
        adopted: list[dict[str, Any]] = []
        if claim.exists():
            if time.time() - _mtime(claim) < LOCK_STALE_S:
                return []  # another flusher is sending these right now
            adopted = _read_rows(claim)
        rows = adopted + _read_rows(path)
        if not rows:
            claim.unlink(missing_ok=True)
            return []
        claim_mtime = _mtime(claim)
        if not _write_claim(claim, rows):
            return []  # the claim could not be written: leave the queue untouched
        if not _clear(path):
            # The claim and the queue would BOTH hold these rows, and whoever adopts the
            # claim later would send every one twice. Roll the claim back to what it held
            # before -- still under the lock -- and send nothing this time.
            _rollback_claim(claim, adopted, claim_mtime)
            return []
        return rows
    except OSError:
        return []
    finally:
        _unlock(path)


def _rollback_claim(claim: Path, adopted: list[dict[str, Any]], mtime: float) -> None:
    """Undo a claim whose queue could not be emptied. Adopted rows go back with their
    old mtime, so a dead flusher's claim stays stale and adoptable."""
    if not adopted:
        _clear(claim, keep_file=False)
        return
    if _write_claim(claim, adopted) and mtime:
        try:
            os.utime(claim, (mtime, mtime))
        except OSError:
            return  # a fresh mtime only delays adoption by LOCK_STALE_S; no row is lost


#: How long a remove or replace retries a Windows sharing violation. Another process
#: reading the file (`pending()` from every session's inbox hook) holds it for
#: milliseconds, and Windows refuses to delete or replace a file while it is open.
SHARE_RETRY_S = 0.5


def _retrying(op: Callable[[], None]) -> bool:
    deadline = time.monotonic() + SHARE_RETRY_S
    while True:
        try:
            op()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        except OSError:
            return False


def _clear(path: Path, *, keep_file: bool = True) -> bool:
    """Make `path` hold no rows. Unlink it; when Windows still refuses after
    SHARE_RETRY_S (a reader holds it open), truncate it IN PLACE -- an open handle
    shares writes, never deletes -- unless `keep_file` is False. True when the file
    is gone or empty."""
    if _retrying(lambda: path.unlink(missing_ok=True)):
        return True
    if not keep_file:
        return False
    try:
        with open(path, "w", encoding="utf-8"):
            pass
        return True
    except OSError:
        return False


def _replace(path: Path, rows: list[dict[str, Any]]) -> bool:
    """Atomically replace `path` with `rows`, keeping an empty file when `rows` is empty.
    When a reader holds `path` open past SHARE_RETRY_S, overwrite it in place instead:
    the caller holds the queue's lock, and a non-atomic write beats a stale file that
    would replay rows already sent."""
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
    except OSError:
        return False
    if _retrying(lambda: os.replace(tmp, path)):
        return True
    try:
        tmp.unlink(missing_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return True
    except OSError:
        return False


def _write_claim(claim: Path, rows: list[dict[str, Any]]) -> bool:
    """Like _rewrite, but an empty claim is kept as an empty FILE (still claimed)."""
    return _replace(claim, rows)


def _release_claim(path: Path, keep: list[dict[str, Any]]) -> None:
    """Put the rows this flush kept back IN FRONT of anything appended meanwhile, then
    drop the claim. Waits out a stale lock rather than strand sent rows in a claim."""
    locked = _lock(path, wait_s=LOCK_STALE_S + LOCK_WAIT_S)
    try:
        if keep and not _rewrite(path, keep + _read_rows(path)):
            return  # the claim stays; a later flush adopts it once it is stale
        # The kept rows now live in the queue. A claim that still held them would be
        # adopted later and resend them, so an undeletable claim is truncated instead.
        _clear(_claim_path(path))
    except OSError:
        return
    finally:
        if locked:
            _unlock(path)


#: How often one host sweeps EVERY queue (see `sweep`). Inbox reads fire at every
#: prompt and tool call of every session; the sweep only needs to run now and then.
SWEEP_INTERVAL_S = 600
_SWEEP_STAMP = ".last-sweep"


def sweep(*, outbox_dir: Optional[Path] = None, now: Optional[float] = None,
          skip: tuple[str, ...] = (), min_interval_s: float = 0.0) -> tuple[int, int]:
    """Archive stale (> MAX_AGE_S) and overflow (> MAX_ROWS) rows in EVERY queue on this
    host, sending nothing. (files_changed, rows_archived).

    A queue is flushed only by a read under its own nick, and most nicks never read
    again: a session that ended with lines queued, or an agent nick a script posted as.
    Measured 2026-10-14: 37 queue files, 36 holding rows days to weeks old. Those rows
    can no longer inform anyone; replaying them would mislead. They are moved to the
    archive file beside the queue -- never deleted -- and FRESH rows of other nicks are
    left for their owner, so a live peer's queue is never sent twice. `skip` names the
    queues this caller is flushing itself (by nick)."""
    outbox_dir = outbox_dir or OUTBOX_DIR
    now = now or time.time()
    stamp = outbox_dir / _SWEEP_STAMP
    if min_interval_s > 0 and now - _mtime(stamp) < min_interval_s:
        return 0, 0
    try:
        files = {p for p in outbox_dir.glob("*.jsonl")
                 if not p.name.endswith(".archived.jsonl")}
        # A claim whose queue is gone (its flusher died after emptying it) is a queue too.
        files |= {p.with_name(p.name[:-len(".flushing")])
                  for p in outbox_dir.glob("*.jsonl.flushing")}
    except OSError:
        return 0, 0
    skipped = {_path(n, outbox_dir).name for n in skip}
    changed = archived_total = 0
    for path in sorted(files):
        if path.name in skipped:
            continue
        gone_n = _sweep_one(path, now)
        if gone_n:
            changed += 1
            archived_total += gone_n
    try:
        outbox_dir.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(now), encoding="utf-8")
    except OSError:
        return changed, archived_total  # unwritable stamp: the next read sweeps again
    return changed, archived_total


def _sweep_one(path: Path, now: float) -> int:
    """Archive one queue's stale and overflow rows under its lock. A busy queue is
    skipped -- its own flusher or appender is live, and the next sweep gets it."""
    if not _lock(path, wait_s=0.0):
        return 0
    try:
        claim = _claim_path(path)
        adopted: list[dict[str, Any]] = []
        if claim.exists():
            if time.time() - _mtime(claim) < LOCK_STALE_S:
                return 0  # a live flusher owns these rows
            adopted = _read_rows(claim)
        rows = adopted + _read_rows(path)
        over = max(0, len(rows) - MAX_ROWS)
        gone = [dict(r, archived="overflow") for r in rows[:over]]
        keep = []
        for row in rows[over:]:
            if now - float(row.get("queued_at") or now) > MAX_AGE_S:
                gone.append(dict(row, archived="stale"))
            else:
                keep.append(row)
        if not gone and not claim.exists():
            return 0  # nothing to do: never rewrite a queue that does not need it
        if gone:
            _archive(path, gone)
        if _rewrite(path, keep):
            _clear(claim)  # its rows now live in the queue (or the archive)
        return len(gone)
    finally:
        _unlock(path)


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _lock(path: Path, wait_s: Optional[float] = None) -> bool:
    """Take the queue's lock (O_CREAT|O_EXCL). False after `wait_s` (default
    LOCK_WAIT_S). A lock older than LOCK_STALE_S belongs to a dead process and is
    broken -- its mtime re-read just before the unlink, so a lock another process
    just re-took is not stolen."""
    lock = _lock_path(path)
    deadline = time.monotonic() + (LOCK_WAIT_S if wait_s is None else wait_s)
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            seen = _mtime(lock)
            if seen and time.time() - seen > LOCK_STALE_S and _mtime(lock) == seen:
                try:
                    lock.unlink()
                except OSError:
                    time.sleep(0.02)  # another breaker won the race; retry the take
                continue
        except OSError:
            return False  # unwritable directory: nothing else can take the lock either
        else:
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _unlock(path: Path) -> None:
    try:
        _lock_path(path).unlink()
    except OSError:
        return  # already gone (broken as stale); nothing to release


def _mtime(path: Path) -> float:
    """0.0 when the file is missing -- "never swept" is due now."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _rewrite(path: Path, rows: list[dict[str, Any]]) -> bool:
    """Atomically replace `path` with `rows` (unlink when empty). False on a disk error:
    the old file stands, so a failed rewrite never loses a row."""
    if not rows:
        return _clear(path)
    return _replace(path, rows)


def _archive(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        arch = path.with_name(path.stem + ".archived.jsonl")
        with open(arch, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        return
