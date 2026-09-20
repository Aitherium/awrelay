"""Register the relay inbox as a Claude Code hook.

    awrelay install-hooks            # ~/.claude/settings.json
    awrelay install-hooks --project  # ./.claude/settings.json
    awrelay install-hooks --uninstall

Nothing is copied anywhere: the hook IS the installed CLI (`python -m awrelay inbox
--claude-hook`), so upgrading the package upgrades the hook and there is no second copy
to drift. Two events:

  SessionStart      tells the session its relay name and what peers said in the last hour
  UserPromptSubmit  delivers what arrived since -- the moment a session next has attention

The entry is matched on its MARKER, never on the whole command string: the interpreter path
differs between a venv and a system python, and matching the full string would install a
second copy every time the environment changed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MARKER = "awrelay inbox --claude-hook"
EVENTS = ("SessionStart", "UserPromptSubmit")
TIMEOUT_S = 12


def command() -> str:
    return '"%s" -m %s' % (sys.executable, MARKER)


def _is_ours(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(MARKER in str(h.get("command", ""))
               for h in entry.get("hooks", []) if isinstance(h, dict))


def merge(data: dict, *, uninstall: bool = False) -> tuple[dict, list[str]]:
    """Pure: (new settings, what changed). Touches nothing but our own entries."""
    changed: list[str] = []
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json `hooks` is not an object")
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError("settings.json hooks.%s is not a list" % event)
        kept = [e for e in entries if not _is_ours(e)]
        had = len(kept) != len(entries)
        if uninstall:
            if had:
                changed.append("removed %s" % event)
            hooks[event] = kept
            continue
        ours = {"hooks": [{"type": "command", "command": command(), "timeout": TIMEOUT_S}]}
        current = [e for e in entries if _is_ours(e)]
        if current == [ours]:
            continue
        hooks[event] = kept + [ours]
        changed.append(("updated %s" if had else "added %s") % event)
    return data, changed


def install(*, user: bool = True, dry_run: bool = False, uninstall: bool = False) -> int:
    target = (Path.home() if user else Path.cwd()) / ".claude" / "settings.json"
    data: dict = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Refuse rather than overwrite: a malformed settings.json is somebody's work
            # in progress, and replacing it is unrecoverable for them.
            print("awrelay: %s is unreadable (%s) -- refusing to touch it" % (target, exc),
                  file=sys.stderr)
            return 1
        if not isinstance(data, dict):
            print("awrelay: %s is not a JSON object -- refusing to touch it" % target,
                  file=sys.stderr)
            return 1
    try:
        data, changed = merge(data, uninstall=uninstall)
    except ValueError as exc:
        print("awrelay: %s" % exc, file=sys.stderr)
        return 1
    if not changed:
        print("awrelay: %s already %s" % (target, "clean" if uninstall else "current"))
        return 0
    for line in changed:
        print("awrelay: %s%s" % ("would have " if dry_run else "", line))
    if dry_run:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".awrelay-tmp%d" % os.getpid())
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    print("awrelay: wrote %s" % target)
    return 0
