"""In-turn delivery: an addressed message reaches a WORKING session at its next
tool call; broadcasts still wait for the prompt; nothing is shown twice.

Measured 2026-09-20: a session heard peers only when its owner next typed --
on a box whose turns run 30-90 minutes that is "real time" in name only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from awrelay import inbox
from awrelay.envelope import Envelope

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp()
ME = "dana+a3b4c1d2"


def row(minute: int, nick: str, kind: str = "finding", text: str = "x", *, to=None,
        hour: int = 11) -> dict:
    payload = {"to": to} if to else {}
    env = Envelope.new(kind, nick, text, payload=payload)
    return {
        "id": f"m{hour}{minute:02d}", "nick": nick,
        "timestamp": f"2026-09-19T{hour:02d}:{minute:02d}:00.000000+00:00",
        "content": env.to_relay_content(),
    }


ROWS = [
    row(10, "lyra+1111aaaa", "finding", "broadcast one"),
    row(11, "lyra+1111aaaa", "request", "for you", to=ME),
    row(12, "hydra+2222bbbb", "finding", "broadcast two"),
    row(13, "hydra+2222bbbb", "request", "also for you", to=ME),
]


def test_direct_only_picks_addressed_rows_and_leaves_broadcasts_for_the_prompt():
    delivered, cursor = inbox.select(ROWS, me=ME, now=NOW, direct_only=True)
    assert [m["text"] for m in delivered] == ["for you", "also for you"]
    assert all(m["direct"] for m in delivered)
    # The in-turn cursor is the newest DIRECT row delivered, not the newest row seen:
    # the prompt-time read must still own "broadcast two".
    assert cursor == ROWS[3]["timestamp"]


def test_prompt_time_read_skips_what_the_turn_already_showed():
    _, direct_cursor = inbox.select(ROWS, me=ME, now=NOW, direct_only=True)
    delivered, cursor = inbox.select(ROWS, me=ME, now=NOW, direct_cursor=direct_cursor)
    assert [m["text"] for m in delivered] == ["broadcast one", "broadcast two"], \
        "an addressed row shown in-turn must not be shown again at the prompt"
    assert cursor == ROWS[3]["timestamp"], "the prompt cursor still advances over every row seen"


def test_a_newer_direct_row_after_the_turn_still_reaches_the_prompt():
    _, direct_cursor = inbox.select(ROWS[:2], me=ME, now=NOW, direct_only=True)
    delivered, _ = inbox.select(ROWS, me=ME, now=NOW, direct_cursor=direct_cursor)
    assert "also for you" in [m["text"] for m in delivered]
    assert "for you" not in [m["text"] for m in delivered]


def test_direct_only_with_nothing_addressed_keeps_its_cursor():
    broadcasts = [ROWS[0], ROWS[2]]
    delivered, cursor = inbox.select(broadcasts, me=ME, now=NOW, cursor="c0", direct_only=True)
    assert delivered == []
    assert cursor == "c0", "nothing delivered -> the in-turn cursor must not move"


def test_inturn_throttle_state_round_trips(tmp_path):
    assert inbox.last_inturn_at(ME, state_dir=tmp_path) == 0.0
    assert inbox.mark_inturn(ME, 1234.5, state_dir=tmp_path)
    assert inbox.last_inturn_at(ME, state_dir=tmp_path) == 1234.5
    # It lives beside the channel cursors, never in their place.
    assert inbox.write_cursor(ME, "#agents", "2026-09-19T11:13:00+00:00", state_dir=tmp_path)
    assert inbox.read_cursor(ME, "#agents", state_dir=tmp_path) == "2026-09-19T11:13:00+00:00"
    assert inbox.last_inturn_at(ME, state_dir=tmp_path) == 1234.5
    assert inbox.direct_key("#agents") != "#agents"
