"""The PostToolUse relay gate: the skip path imports nothing heavy; delivery unchanged.

Measured 2026-09-26: the in-turn hook cost ~2.9 s median per tool call because its
throttle ran after `python -m awrelay` imported httpx and the client. These tests pin
the gate's contract: skip inside the window without running the read, run the read
(once, with the same stdin) when the window has elapsed, keep presence and the
statusline cache consistent, and never exit non-zero.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from awrelay import cli, hookgate, inbox
from awrelay.envelope import Envelope

SID = "a3b4c1d2-9f00-4c11-8e21-77aa00ff1234"
GATE = Path(hookgate.__file__).resolve()


def _payload(sid: str = SID, event: str = "PostToolUse", **kw) -> str:
    return json.dumps({"session_id": sid, "hook_event_name": event, **kw})


def test_first_call_runs_the_read_and_stamps(tmp_path):
    calls = []
    rc = hookgate.main(["--min-interval", "20"], stdin_text=_payload(), now=1000.0,
                       state_dir=tmp_path, runner=lambda raw, extra: calls.append(raw))
    assert rc == 0
    assert calls == [_payload()], "the read must receive the SAME hook stdin"
    assert hookgate.stamp_path(SID, tmp_path).stat().st_mtime == 1000.0


def test_inside_the_window_skips_without_running_the_read(tmp_path):
    hookgate.mark(SID, 1000.0, tmp_path)
    calls = []
    rc = hookgate.main(["--min-interval", "20"], stdin_text=_payload(), now=1019.0,
                       state_dir=tmp_path, runner=lambda raw, extra: calls.append(raw))
    assert rc == 0 and calls == []
    assert hookgate.stamp_path(SID, tmp_path).stat().st_mtime == 1000.0, \
        "a skip must not push the window forward"


def test_window_elapsed_runs_again(tmp_path):
    hookgate.mark(SID, 1000.0, tmp_path)
    calls = []
    hookgate.main(["--min-interval", "20"], stdin_text=_payload(), now=1020.5,
                  state_dir=tmp_path, runner=lambda raw, extra: calls.append(raw))
    assert len(calls) == 1


def test_throttle_is_per_session(tmp_path):
    hookgate.mark(SID, 1000.0, tmp_path)
    calls = []
    hookgate.main([], stdin_text=_payload(sid="ffff0000-1111"), now=1001.0,
                  state_dir=tmp_path, runner=lambda raw, extra: calls.append(raw))
    assert len(calls) == 1, "another session's stamp must not throttle this one"


def test_a_failing_read_never_fails_the_tool_call(tmp_path):
    def boom(raw, extra):
        raise RuntimeError("relay down")
    assert hookgate.main([], stdin_text=_payload(), now=1.0, state_dir=tmp_path,
                         runner=boom) == 0
    assert hookgate.main([], stdin_text="not json", now=100.0, state_dir=tmp_path,
                         runner=boom) == 0


def test_the_passing_read_runs_unthrottled_and_in_turn(tmp_path, monkeypatch):
    """The gate IS the throttle: the CLI must not apply a second --min-interval, or its
    own later mark would swallow a legitimate window."""
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        seen["stdin"] = sys.stdin.read()
        return 0
    monkeypatch.setattr(cli, "main", fake_main)
    hookgate._run_inturn_read(_payload(), ["--channel", "#x"])
    assert seen["argv"][:5] == ["inbox", "--claude-hook", "--direct-only",
                                "--min-interval", "0"]
    assert seen["argv"][5:] == ["--channel", "#x"]
    assert seen["stdin"] == _payload()


def test_skip_path_imports_nothing_heavy(tmp_path):
    """The whole point: run by path with -S -I inside the window, no httpx, no awrelay."""
    env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    state = tmp_path / ".aither" / "relay-inbox"
    hookgate.mark(SID, time.time(), state)
    proc = subprocess.run(
        [sys.executable, "-S", "-I", "-X", "importtime", str(GATE), "--min-interval", "600"],
        input=_payload(), capture_output=True, text=True, encoding="utf-8", env=env,
        timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout == ""
    assert "import time:" in proc.stderr, "importtime printed nothing: the check is blind"
    # Nested imports are indented after the bar ("|   httpx._api"), so match any depth.
    hits = re.findall(r"\|\s+(httpx|awrelay|site)(?:\.|\s*$)", proc.stderr, re.M)
    assert not hits, f"imported on the skip path: {sorted(set(hits))}"


def test_session_end_clears_everything(tmp_path):
    hookgate.mark(SID, 5.0, tmp_path)
    hookgate.write_status(SID, nick="dana+a3b4c1d2", channel="#agents", unread=2,
                          state_dir=tmp_path)
    hookgate.write_presence(SID, nick="dana+a3b4c1d2", cwd=str(tmp_path), state_dir=tmp_path)
    hookgate.main(["--session-end"], stdin_text=_payload(event="SessionEnd"),
                  state_dir=tmp_path, runner=lambda *a: None)
    assert not hookgate.stamp_path(SID, tmp_path).exists()
    assert hookgate.read_status(SID, tmp_path) is None
    assert hookgate.live_sessions(state_dir=tmp_path) == []


def test_presence_expires_and_heartbeat_keeps_it_alive(tmp_path):
    hookgate.write_presence(SID, nick="dana+a3b4c1d2", cwd=str(tmp_path), branch="feat/x",
                            now=1000.0, state_dir=tmp_path)
    live = hookgate.live_sessions(now=1000.0 + 60, state_dir=tmp_path)
    assert [(s["nick"], s["branch"]) for s in live] == [("dana+a3b4c1d2", "feat/x")]
    # A tool call (skip path included) refreshes the heartbeat.
    hookgate.main([], stdin_text=_payload(), now=1000.0 + hookgate.PRESENCE_TTL_S,
                  state_dir=tmp_path, runner=lambda *a: None)
    assert hookgate.live_sessions(now=1000.0 + hookgate.PRESENCE_TTL_S + 60,
                                  state_dir=tmp_path)
    # Idle past the TTL: gone, and the stale file is pruned.
    later = 1000.0 + 3 * hookgate.PRESENCE_TTL_S
    assert hookgate.live_sessions(now=later, state_dir=tmp_path) == []
    assert not hookgate.presence_path(SID, tmp_path).exists()


def test_git_branch_reads_head_and_worktree_gitfile(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/cc-overhaul\n", encoding="utf-8")
    (repo / "sub").mkdir()
    assert hookgate.git_branch(str(repo / "sub")) == "feat/cc-overhaul"
    wt = tmp_path / "wt"
    wt.mkdir()
    gd = tmp_path / "gitdirs" / "wt"
    gd.mkdir(parents=True)
    (gd / "HEAD").write_text("0123456789abcdef\n", encoding="utf-8")
    (wt / ".git").write_text(f"gitdir: {gd}\n", encoding="utf-8")
    assert hookgate.git_branch(str(wt)) == "0123456789", "detached HEAD reads as a short sha"


# ── the CLI side: the hook that reads the relay writes the statusline cache ──

ME = "dana+a3b4c1d2"


def _row(minute: int, nick: str, kind: str, text: str, to=None) -> dict:
    env = Envelope.new(kind, nick, text, payload={"to": to} if to else {})
    return {"id": f"m{minute}", "nick": nick,
            "timestamp": f"2099-09-19T11:{minute:02d}:00.000000+00:00",
            "content": env.to_relay_content()}


ROWS = [
    _row(10, "lyra+1111aaaa", "finding", "broadcast one"),
    _row(11, "lyra+1111aaaa", "request", "for you", to=ME),
    _row(12, "hydra+2222bbbb", "finding", "broadcast two"),
]


class _FakeClient:
    identity_nick = "dana"
    nick = ME

    def __init__(self):
        self._client = type("T", (), {"timeout": 0})()

    def history(self, channel, limit=80):
        return list(ROWS)


def _run_hook(monkeypatch, tmp_path, argv, event):
    monkeypatch.setattr(cli, "_client_from_args", lambda args: _FakeClient())
    monkeypatch.setattr(cli, "_flush_outbox", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_hook_log", lambda msg: None)
    monkeypatch.setattr(inbox, "STATE_DIR", tmp_path)
    for fn in ("read_cursor", "write_cursor"):
        orig = getattr(inbox, fn)
        monkeypatch.setattr(inbox, fn, lambda *a, _o=orig, **k: _o(*a, state_dir=tmp_path, **k))
    monkeypatch.setattr(hookgate, "STATE_DIR", tmp_path)
    for fn in ("write_status", "write_presence"):
        orig = getattr(hookgate, fn)
        monkeypatch.setattr(hookgate, fn, lambda *a, _o=orig, **k: _o(*a, state_dir=tmp_path, **k))
    orig_hb = hookgate.heartbeat
    monkeypatch.setattr(hookgate, "heartbeat", lambda sid, now: orig_hb(sid, now, tmp_path))
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(_payload(event=event, cwd=str(tmp_path))))
    monkeypatch.delenv("AWRELAY_SESSION_ID", raising=False)
    return cli.main(argv)


def test_inturn_read_caches_the_broadcasts_still_queued(monkeypatch, tmp_path, capsys):
    rc = _run_hook(monkeypatch, tmp_path,
                   ["inbox", "--claude-hook", "--direct-only", "--min-interval", "0"],
                   "PostToolUse")
    assert rc == 0
    assert "for you" in capsys.readouterr().out
    status = hookgate.read_status(SID, tmp_path)
    assert status is not None and status["unread"] == 2, \
        "two broadcasts wait for the prompt; the direct one was just delivered"


def test_prompt_read_zeroes_the_cache_and_session_start_writes_presence(
        monkeypatch, tmp_path, capsys):
    rc = _run_hook(monkeypatch, tmp_path, ["inbox", "--claude-hook"], "SessionStart")
    assert rc == 0
    assert hookgate.read_status(SID, tmp_path)["unread"] == 0
    live = hookgate.live_sessions(state_dir=tmp_path)
    assert [(s["nick"], s["cwd"]) for s in live] == [(ME, str(tmp_path))]


def test_hooks_disabled_env_makes_the_gate_a_noop(tmp_path, monkeypatch):
    """A probe timing the hook (adk claude doctor) must not heartbeat, stamp or read."""
    monkeypatch.setenv("AWRELAY_HOOKS_DISABLED", "1")
    calls = []
    rc = hookgate.main(["--min-interval", "0"], stdin_text=_payload(), now=1000.0,
                       state_dir=tmp_path, runner=lambda raw, extra: calls.append(raw))
    assert rc == 0 and calls == []
    assert not any(tmp_path.rglob("*")), "a disabled hook wrote state"


def test_hooks_disabled_env_makes_inbox_claude_hook_a_noop(monkeypatch):
    monkeypatch.setenv("AWRELAY_HOOKS_DISABLED", "1")

    built = []

    def no_network(*_a, **_k):  # the hook swallows exceptions, so RECORD, never raise
        built.append(1)
        raise RuntimeError("offline")

    monkeypatch.setattr(cli, "_client_from_args", no_network)
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(_payload(event="UserPromptSubmit")))
    assert cli.main(["inbox", "--claude-hook"]) == 0
    assert built == [], "a disabled hook built a relay client"
