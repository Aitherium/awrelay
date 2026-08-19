"""Envelope — the structured shape an agent message carries inside a plain
AitherRelay chat message.

AitherRelay's own wire format is a channel + a human-readable `content`
string — it has no notion of "this is a finding" vs "this is small talk".
Envelope rides on top of that deliberately, rather than asking the relay
server to grow a new message type: `content` becomes a fenced JSON block
(``` awrelay ... ```) with a plain-English summary line ABOVE the fence, so
a human reading the channel sees a sentence, and an `awrelay`-aware reader
sees structured data. A relay server that has never heard of awrelay still
serves the message correctly — it just looks like a code-fenced chat post.

Fields:
    kind        one of MESSAGE, FINDING, ALERT, REQUEST, STEER, ACK — see
                the Kind enum for what each means and when to use it.
    sender      the agent's own identifier (its relay nick).
    text        one-line, human-readable summary. Always required: an
                Envelope with no readable text is exactly the failure mode
                this module exists to avoid — a human scrolling the channel
                must be able to tell what happened without decoding JSON.
    payload     structured data specific to `kind` — e.g. a FINDING's
                file/line/summary, a REQUEST's args. Free-form dict; callers
                agree on shape out of band, same as any chat protocol's
                message body.
    correlation_id  ties a REQUEST to its ACK, or a follow-up FINDING to the
                one it refines. None for a standalone message.
    sent_at     UTC ISO-8601 timestamp, set by `Envelope.new()`.

NOT an A2A (Agent2Agent) implementation. The shapes are deliberately close
enough that a bridge is a translation layer, not a rewrite — `kind` maps
loosely onto A2A's message/task/artifact split — but this does not claim
protocol compliance, and does not route through this repo's own A2A layer.
That layer has a known, separate gap (an in-fleet caller's internal key
bypasses per-skill grants — see AitherOS memory
`a2a-internal-key-bypasses-per-skill-grants`), and bridging into it before
that is fixed would extend the hole rather than fix it. A real A2A bridge
is future work, tracked as such, not silently skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

_FENCE_LANG = "awrelay"
_FENCE_RE = re.compile(
    r"```" + _FENCE_LANG + r"\n(?P<body>.*?)\n```", re.DOTALL
)


class Kind(str, Enum):
    """What an Envelope is for. Keep this short — a growing enum here is a
    sign the payload should carry the distinction instead."""

    #: Ordinary agent-to-agent chat. No reply expected.
    MESSAGE = "message"
    #: "I found something" — a concrete result worth another agent's
    #: attention. `payload` should be self-contained enough to act on
    #: without re-reading the sender's own session.
    FINDING = "finding"
    #: Something is wrong and someone should look. Distinct from FINDING:
    #: an alert names a problem, a finding names a result.
    ALERT = "alert"
    #: Asking another agent to do something. Pair with `correlation_id` so
    #: the eventual ACK (or a FINDING answering it) can be matched back.
    REQUEST = "request"
    #: Redirect a running agent — "stop that, do this instead". Mirrors
    #: the decision-card `store.steer()` semantics: does not close
    #: whatever it interrupts, just changes its direction.
    STEER = "steer"
    #: Acknowledges a REQUEST by `correlation_id`. `payload` may carry the
    #: result, or be empty for a bare "got it".
    ACK = "ack"


@dataclass
class Envelope:
    kind: Kind
    sender: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    sent_at: Optional[str] = None

    @classmethod
    def new(
        cls,
        kind: Kind | str,
        sender: str,
        text: str,
        payload: Optional[dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> "Envelope":
        """Build an Envelope stamped with the current UTC time.

        `kind` accepts a bare string (`"finding"`) as well as `Kind.FINDING`
        so a caller need not import the enum for the common case.
        """
        if not text or not text.strip():
            raise ValueError(
                "Envelope.text is required — a message a human cannot read "
                "at a glance defeats the point of riding on a chat channel"
            )
        return cls(
            kind=Kind(kind),
            sender=sender,
            text=text.strip(),
            payload=payload or {},
            correlation_id=correlation_id,
            sent_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_relay_content(self) -> str:
        """Render as the `content` string AitherRelay's `/messages` POST
        expects: a human-readable line, then the structured body fenced so
        it round-trips exactly through `from_relay_content`.
        """
        body = json.dumps(
            {
                "kind": self.kind.value,
                "sender": self.sender,
                "payload": self.payload,
                "correlation_id": self.correlation_id,
                "sent_at": self.sent_at,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        prefix = {
            Kind.MESSAGE: "",
            Kind.FINDING: "[finding] ",
            Kind.ALERT: "[alert] ",
            Kind.REQUEST: "[request] ",
            Kind.STEER: "[steer] ",
            Kind.ACK: "[ack] ",
        }[self.kind]
        return f"{prefix}{self.text}\n```{_FENCE_LANG}\n{body}\n```"

    @classmethod
    def from_relay_content(cls, content: str) -> Optional["Envelope"]:
        """Parse a relay message's `content` back into an Envelope, or
        `None` if it carries no awrelay fence — an ordinary chat message
        from a human or a non-awrelay client, not a parse failure.
        """
        match = _FENCE_RE.search(content or "")
        if not match:
            return None
        try:
            data = json.loads(match.group("body"))
        except json.JSONDecodeError:
            return None
        try:
            kind = Kind(data["kind"])
        except (KeyError, ValueError):
            return None
        text = content[: match.start()].strip()
        # Strip the bracketed kind prefix to_relay_content added, so a
        # round-trip through to_relay_content/from_relay_content is exact.
        text = re.sub(r"^\[(finding|alert|request|steer|ack)\]\s*", "", text)
        return cls(
            kind=kind,
            sender=data.get("sender", ""),
            text=text,
            payload=data.get("payload") or {},
            correlation_id=data.get("correlation_id"),
            sent_at=data.get("sent_at"),
        )
