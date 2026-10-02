"""The cheap half of the Claude Code relay hooks: stdlib only, no package import.

WHY THIS FILE EXISTS
--------------------
The in-turn read (`awrelay inbox --claude-hook --direct-only --min-interval 20`) runs
on EVERY PostToolUse. Its throttle sat inside `_cmd_inbox`, i.e. after `python -m
awrelay` had imported httpx, the client, the doctor intercept and awgit -- measured
2026-09-26 at a ~2.9 s median per tool call, for a hook that skips 19 times in 20.

This module is run BY PATH with ``python -S -I`` so the skip path costs one
interpreter start plus a ``stat``: no site-packages, no awrelay/__init__, no httpx.
Only when the interval has elapsed does it turn site back on and hand the SAME
stdin to the real CLI in-process (no second interpreter).

    python -S -I <pkg>/awrelay/hookgate.py --min-interval 20      # PostToolUse
    python -S -I <pkg>/awrelay/hookgate.py --session-end          # SessionEnd

It also owns the three small files the hooks and the statusline share, all under
``~/.aither/relay-inbox/`` and keyed by the Claude Code session id:

  * ``inturn/<sid>.stamp``   mtime = last in-turn relay read (the throttle)
  * ``status/<sid>.json``    {"unread": N, ...} -- peer rows queued for the next
                             prompt, written by the hook, read by the statusline
                             (no network on the render path)
  * ``presence/<sid>.json``  this session's nick + cwd + branch; its mtime is the
                             heartbeat (touched on every tool call, skip path too);
                             stale after PRESENCE_TTL_S, removed on SessionEnd.

Delivery semantics are unchanged: when this gate passes it runs the in-turn read
with ``--min-interval 0`` (the gate IS the throttle; a second one inside would see
its own later mark and skip a legitimate window).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".aither" / "relay-inbox"
#: Set by probes that RUN the hook commands (e.g. `adk claude doctor` timing them): every
#: hook entrypoint returns 0 at once -- no heartbeat, no stamp, no inbox read or drain.
HOOKS_DISABLED_ENV = "AWRELAY_HOOKS_DISABLED"


def hooks_disabled() -> bool:
    return os.environ.get(HOOKS_DISABLED_ENV, "").strip().lower() in ("1", "true", "yes")

DEFAULT_INTERVAL_S = 20.0
#: A session that has not run a tool or taken a prompt for this long is not "live".
PRESENCE_TTL_S = 30 * 60
#: The statusline shows a count older than this as stale rather than current.
STATUS_STALE_S = 10 * 60


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "+-_" else "_" for c in (name or "nosession"))[:80]


def session_id_of(data: dict) -> str:
    """AWRELAY_SESSION_ID wins, as it does in the CLI, so the gate and the read agree."""
    return (os.environ.get("AWRELAY_SESSION_ID", "").strip()
            or str(data.get("session_id") or "").strip())


# ── throttle ────────────────────────────────────────────────────────────────

def stamp_path(sid: str, state_dir: Path = STATE_DIR) -> Path:
    return state_dir / "inturn" / (_safe(sid) + ".stamp")


def should_skip(sid: str, interval: float, now: float, state_dir: Path = STATE_DIR) -> bool:
    """True when this session read the relay in-turn less than `interval` seconds ago."""
    if interval <= 0:
        return False
    try:
        age = now - stamp_path(sid, state_dir).stat().st_mtime
    except OSError:
        return False
    return 0 <= age < interval


def mark(sid: str, now: float, state_dir: Path = STATE_DIR) -> bool:
    path = stamp_path(sid, state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (now, now))
    except OSError:
        return False
    return True


# ── unread cache (statusline) ───────────────────────────────────────────────

def status_path(sid: str, state_dir: Path = STATE_DIR) -> Path:
    return state_dir / "status" / (_safe(sid) + ".json")


def write_status(sid: str, *, nick: str, channel: str, unread: int, now: float | None = None,
                 state_dir: Path = STATE_DIR) -> bool:
    if not sid:
        return False
    path = status_path(sid, state_dir)
    rec = {"nick": nick, "channel": channel, "unread": int(unread),
           "at": time.time() if now is None else now}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def read_status(sid: str, state_dir: Path = STATE_DIR) -> dict | None:
    try:
        data = json.loads(status_path(sid, state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── presence (local registry) ───────────────────────────────────────────────

def presence_path(sid: str, state_dir: Path = STATE_DIR) -> Path:
    return state_dir / "presence" / (_safe(sid) + ".json")


def git_branch(cwd: str) -> str:
    """The branch checked out at `cwd`, read from HEAD -- no git subprocess."""
    try:
        here = Path(cwd).resolve()
    except (OSError, ValueError):
        return ""
    for d in [here, *here.parents]:
        dot = d / ".git"
        try:
            if dot.is_file():
                line = dot.read_text(encoding="utf-8").strip()
                if not line.startswith("gitdir:"):
                    return ""
                gd = Path(line[len("gitdir:"):].strip())
                dot = gd if gd.is_absolute() else (d / gd)
            if dot.is_dir():
                head = (dot / "HEAD").read_text(encoding="utf-8").strip()
                if head.startswith("ref:"):
                    ref = head[4:].strip()
                    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
                return head[:10]
        except OSError:
            return ""
    return ""


def _host() -> str:
    import socket
    try:
        return socket.gethostname()
    except OSError:
        return os.environ.get("COMPUTERNAME", "")


def write_presence(sid: str, *, nick: str, cwd: str, branch: str = "", channel: str = "",
                   now: float | None = None, state_dir: Path = STATE_DIR) -> bool:
    if not sid:
        return False
    now = time.time() if now is None else now
    rec = {"session_id": sid, "nick": nick, "cwd": cwd,
           "branch": branch if branch else git_branch(cwd), "channel": channel,
           "started_at": now, "host": _host()}
    path = presence_path(sid, state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, path)
        os.utime(path, (now, now))
    except OSError:
        return False
    return True


def heartbeat(sid: str, now: float, state_dir: Path = STATE_DIR) -> None:
    """Refresh this session's presence mtime; a missing record stays missing."""
    try:
        os.utime(presence_path(sid, state_dir), (now, now))
    except OSError:
        return  # no record (never started, or ended): nothing to keep alive


def clear_session(sid: str, state_dir: Path = STATE_DIR) -> int:
    """SessionEnd: drop presence, status and stamp. Returns files removed."""
    n = 0
    for p in (presence_path(sid, state_dir), status_path(sid, state_dir),
              stamp_path(sid, state_dir)):
        try:
            p.unlink()
        except OSError:
            continue
        n += 1
    return n


def live_sessions(now: float | None = None, ttl: float = PRESENCE_TTL_S,
                  state_dir: Path = STATE_DIR, prune: bool = True) -> list[dict]:
    """Presence records whose heartbeat is within `ttl`; expired ones are pruned."""
    now = time.time() if now is None else now
    out: list[dict] = []
    try:
        files = sorted((state_dir / "presence").glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            age = now - f.stat().st_mtime
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if age > ttl:
            if prune:
                with contextlib.suppress(OSError):
                    f.unlink()
            continue
        rec["last_seen_s"] = round(max(0.0, age), 1)
        out.append(rec)
    return out


# ── entry point ─────────────────────────────────────────────────────────────

def _run_inturn_read(raw: str, extra: list[str]) -> int:
    """The slow path: turn site back on and run the real CLI in THIS process."""
    pkg_root = str(Path(__file__).resolve().parent.parent)
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    if sys.flags.no_site:
        import site
        site.main()
    from awrelay.cli import main as cli_main
    sys.stdin = io.StringIO(raw)
    return int(cli_main(["inbox", "--claude-hook", "--direct-only", "--min-interval", "0",
                         *extra]) or 0)


def main(argv: list[str] | None = None, *, stdin_text: str | None = None,
         now: float | None = None, state_dir: Path = STATE_DIR, runner=None) -> int:
    """Hook contract: never a non-zero exit, never a traceback on stdout."""
    if hooks_disabled():
        return 0
    args = list(sys.argv[1:] if argv is None else argv)
    interval = DEFAULT_INTERVAL_S
    session_end = False
    extra: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--min-interval" and i + 1 < len(args):
            try:
                interval = float(args[i + 1])
            except ValueError:
                interval = DEFAULT_INTERVAL_S  # a typo in settings keeps the default window
            i += 2
            continue
        if a == "--session-end":
            session_end = True
        elif a == "--channel" and i + 1 < len(args):
            extra += [a, args[i + 1]]
            i += 2
            continue
        i += 1
    try:
        raw = sys.stdin.read() if stdin_text is None else stdin_text
    except (OSError, ValueError):
        raw = ""
    try:
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    sid = session_id_of(data)
    now = time.time() if now is None else now
    if session_end:
        if sid:
            clear_session(sid, state_dir)
        return 0
    if sid:
        heartbeat(sid, now, state_dir)
    if should_skip(sid, interval, now, state_dir):
        return 0
    mark(sid, now, state_dir)
    try:
        (runner or _run_inturn_read)(raw, extra)
    except BaseException:  # noqa: BLE001 - a relay hook must never fail a tool call
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
