"""The session-alias convention: what it must ADMIT, and what it must REFUSE.

Most of these are refusals on purpose. The rule's whole safety argument is that
an alias cannot become anyone else, so a suite that only proved the happy path
would be proving the uninteresting half.
"""
from __future__ import annotations

import pytest
from awrelay.session import (
    current_session_id,
    is_session_alias,
    session_nick,
)

# --- what it must ADMIT ------------------------------------------------------

@pytest.mark.parametrize("req", ["dana+a3b4c1d2", "dana+claude-a3b4", "dana+s1x"])
def test_admits_an_alias_of_your_own_nick(req):
    assert is_session_alias(req, "dana")


# --- what it must REFUSE, which is the point ---------------------------------

def test_refuses_a_different_nick():
    assert not is_session_alias("sam", "dana")


def test_refuses_a_longer_nick_sharing_the_prefix():
    """A bare startswith() would have admitted this -- the separator is the rule."""
    assert not is_session_alias("danabot", "dana")
    assert not is_session_alias("dana-2", "dana")


def test_refuses_aliasing_someone_elses_nick():
    assert not is_session_alias("sam+x1y2", "dana")


def test_the_plain_nick_is_not_an_alias():
    """The server's own equality check owns that case; overlapping would make
    two rules responsible for one decision."""
    assert not is_session_alias("dana", "dana")


@pytest.mark.parametrize("bad", [
    "dana+",              # empty suffix
    "dana+a",             # one char: too collidable
    "dana+a+b",           # a second separator: dana+a could claim dana+a+b
    "dana+has space",
    "dana+@everyone",     # mention-shaped
    "dana+x.y",
    "dana+" + "z" * 60,   # over long
])
def test_refuses_malformed_suffixes(bad):
    assert not is_session_alias(bad, "dana")


def test_empty_inputs_refuse_rather_than_crash():
    assert not is_session_alias("", "dana")
    assert not is_session_alias("dana+x1", "")


# --- the generator and the validator must agree ------------------------------

def test_session_nick_produces_what_is_session_alias_admits():
    n = session_nick("dana", "a3b4c1d2-9f00-4c11-8e21-77aa00ff1234")
    assert n == "dana+a3b4c1d2"
    assert is_session_alias(n, "dana")


def test_session_nick_takes_the_whole_first_block():
    """A blind truncation gives `dana+a3b4c1d2-9f0`, which ends mid-block and
    reads as if it were cut off, because it was."""
    assert not session_nick("dana", "a3b4c1d2-9f00-4c11").endswith("-9f0")


@pytest.mark.parametrize("junk", ["", "!!!", "---", None])
def test_an_unusable_id_degrades_to_the_plain_nick(junk):
    """Indistinguishable is recoverable; two sessions colliding on one
    malformed alias is not."""
    assert session_nick("dana", junk) == "dana"


# --- reading the id from the environment -------------------------------------

def test_current_session_id_prefers_the_explicit_var(monkeypatch):
    monkeypatch.setenv("AWRELAY_SESSION_ID", "explicit")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "fallback")
    assert current_session_id() == "explicit"


def test_current_session_id_is_empty_rather_than_invented(monkeypatch):
    """A fabricated id would change every run, so a reader could not tell two
    messages from one session apart from two different sessions."""
    for var in ("AWRELAY_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "AGENT_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    assert current_session_id() == ""
