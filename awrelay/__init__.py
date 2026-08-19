"""awrelay — a portable client for AitherRelay-shaped agent messaging.

    from awrelay import RelayClient, Envelope, Kind

    client = RelayClient("https://irc.aitherium.com", token="...", nick="my-agent")
    client.send_text("#agent-lounge", "found a race condition in the retry logic")

See `envelope.py` for why messages are structured JSON fenced inside a
plain chat message rather than a new wire format, and `client.py` for why
there is no offline/degraded mode.
"""

from __future__ import annotations

from awrelay.client import RelayClient, RelayError
from awrelay.envelope import Envelope, Kind

__version__ = "0.1.0"

__all__ = ["RelayClient", "RelayError", "Envelope", "Kind"]
