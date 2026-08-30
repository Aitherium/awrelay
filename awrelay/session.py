"""One identity, many concurrent session nicks — `<your-nick>+<session>`.

WHAT THIS IS FOR
----------------
Several agent sessions belonging to one person want to talk in the same channel
and be told apart: who is doing what, who to ask, who to deconflict an edit
with. Relay servers commonly bind a caller to ONE nick — the one its credential
authenticates as — so without a convention every session posts as the same name
and they are mutually invisible.

An alias solves that without a new credential per session. The prefix is the
AUTHENTICATED nick and the caller supplies only a suffix, so:

  * `dana+a3b4c1d2` is provably dana, and cannot become `sam`;
  * it cannot become `danabot` either — the separator makes the boundary
    unambiguous, which a bare ``startswith`` would not;
  * it grants NOTHING the identity did not already have. Whatever `dana` could
    do, an alias of dana can do; whatever dana could not, it still cannot. This
    lets one identity hold several concurrent nicks. It does not widen any of
    them.

That last point is the whole safety argument, and it is why this is a NICK
convention rather than a permission change.

WHY THE SUFFIX CHARSET IS NARROW
--------------------------------
A nick lands in channel rosters, DM routing and @mention matching. Anything that
could collide with a separator, a mention pattern or another nick is REFUSED
rather than sanitised — sanitising quietly maps two different sessions onto one
nick, and they then believe they are distinct while the server treats them as
one, which is worse than refusing outright.

    >>> session_nick("dana", "a3b4c1d2-9f00-4c11-8e21-77aa00ff1234")
    'dana+a3b4c1d2'
    >>> is_session_alias("dana+a3b4c1d2", "dana")
    True
    >>> is_session_alias("danabot", "dana")
    False

A SERVER adopting this checks ``is_session_alias`` alongside its equality test;
a CLIENT uses ``session_nick`` to pick the nick to ask for. Both sides use the
same two functions, so they cannot disagree about what an alias is.
"""

from __future__ import annotations

import os
import re

__all__ = ["SEP", "is_session_alias", "session_nick", "current_session_id"]

SEP = "+"

#: Suffix charset: starts alphanumeric, then alphanumerics and dashes, 2-40
#: chars. No dot, no underscore, no `@`, no second separator.
_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{1,39}$")

#: Environment variables that carry a session identifier, most specific first.
_SESSION_ENV = ("AWRELAY_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "AGENT_SESSION_ID")


def is_session_alias(req_nick: str, identity_nick: str) -> bool:
    """Is ``req_nick`` a session-scoped alias of ``identity_nick``?

    Pure and FAIL-CLOSED: every unexpected shape returns False, so a server that
    wires this as ``exact_match or is_session_alias(...)`` can only ever widen to
    the aliases this function positively recognises.
    """
    if not req_nick or not identity_nick:
        return False
    prefix = identity_nick + SEP
    if not req_nick.startswith(prefix):
        return False
    suffix = req_nick[len(prefix):]
    if SEP in suffix:
        return False
    return bool(_SUFFIX_RE.match(suffix))


def session_nick(identity_nick: str, session_id: str) -> str:
    """The nick this session should ask for. Shortens the id, never the identity.

    Prefers the FIRST dash-delimited block of a UUID. A blind truncation of
    ``a3b4c1d2-9f00-...`` yields ``a3b4c1d2-9f0`` — a nick that ends mid-block
    and reads as if it were cut off, because it was. The first block is enough
    to tell apart the few sessions one person runs at once.

    An id with nothing usable in it returns the PLAIN nick rather than a
    half-formed alias: degrading to "indistinguishable" is recoverable, whereas
    two sessions colliding on one malformed alias is not.
    """
    raw = str(session_id or "")
    head = raw.split("-")[0]
    short = re.sub(r"[^A-Za-z0-9-]", "", head if len(head) >= 6 else raw)[:12]
    if not short or not _SUFFIX_RE.match(short):
        return identity_nick
    return identity_nick + SEP + short


def current_session_id() -> str:
    """This process's session id from the environment, or "" if it has none.

    Returning "" rather than inventing one is deliberate: a fabricated id would
    change on every run, so a reader could not tell two messages from the same
    session apart from two different sessions.
    """
    for var in _SESSION_ENV:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""
