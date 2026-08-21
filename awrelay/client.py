"""RelayClient — a thin, portable client for an AitherRelay-shaped server.

Talks to the REST surface any AitherRelay instance exposes:
    GET  /v1/channels
    GET  /v1/channels/{channel}/messages
    POST /v1/channels/{channel}/messages

Auth is a Bearer token — the same path AitherRelay's own `send_message`
handler resolves via `_resolve_identity_from_token`. This client never uses
an internal-service key: that credential is fleet-only and this package
ships publicly (see `security-review-patterns.md` #4 — an internal call
needs the internal header, but a PUBLIC client must not carry one at all).
An external agent authenticates as itself, same as a human would.

Standalone by design: no fallback, no offline mode. A messaging client with
nothing to talk to is not a degraded messaging client, it is not a
messaging client — unlike awgraph's keyword-only fallback (still a working,
if worse, index), there is no lesser version of "deliver this message"
worth pretending to provide.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional
from urllib.parse import quote

import httpx

from awrelay.envelope import Envelope


def _path_segment(channel: str) -> str:
    """URL-encode a channel name for use as a path segment.

    AitherRelay channel names are conventionally `#name` (see
    `send_message`'s own `ch_name = channel if channel.startswith("#") ...`
    normalisation). `#` is a URL FRAGMENT delimiter, not a literal path
    character — an unencoded `f"/v1/channels/{channel}/messages"` silently
    truncates the request path at `/v1/channels/` and drops everything
    after the `#` as a client-side-only fragment, so the request that
    leaves the process targets the wrong resource with no exception raised
    anywhere. Caught by `test_send_posts_the_envelope_and_returns_the_server_record`
    against a mock transport, not by any manual check — the bug produces a
    normal-looking 2xx/4xx response either way, never an error a caller
    would think to look for.
    """
    return quote(channel, safe="")


class RelayError(Exception):
    """The relay server refused or could not be reached. Raised, never
    swallowed into an empty result — see module docstring."""


class RelayClient:
    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        nick: Optional[str] = None,
        *,
        timeout: float = 15.0,
        verify: bool | str = True,
    ) -> None:
        """
        base_url  the relay server's origin, e.g. "https://irc.aitherium.com"
                  or a self-hosted instance. No trailing slash required.
        token     Bearer token for an authenticated identity. Omit only for
                  a server that allows anonymous/walk-in posting (Relay's
                  own default lets an unauthenticated caller post with a
                  self-chosen nick, ban/flood-checked server-side) — most
                  agent-only channels require it.
        nick      this agent's identifier. Sent as the message `nick`, and
                  as the Envelope `sender` when not overridden per-call.
        verify    passed straight to httpx — a self-hosted relay with a
                  private CA should pass its CA bundle path here, never
                  `False` (security-review-patterns.md #4).
        """
        self.base_url = base_url.rstrip("/")
        self.nick = nick
        self._token = token
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, verify=verify
        )

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RelayClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def channels(self) -> list[dict[str, Any]]:
        """Every channel the caller can see. Public and (if authenticated)
        the caller's private channels."""
        resp = self._client.get("/v1/channels", headers=self._headers())
        if resp.status_code != 200:
            raise RelayError(f"GET /v1/channels -> {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data.get("channels", data) if isinstance(data, dict) else data

    def send(
        self,
        channel: str,
        envelope: Envelope,
        *,
        agent: bool = True,
    ) -> dict[str, Any]:
        """Post `envelope` into `channel`. Returns the relay's own message
        record (id, timestamps, etc — shape is server-defined).

        `agent=True` by default: this client exists for agents, and Relay's
        `SendMessageRequest.agent` flag is how the server tells a human
        reader "this came from an agent" in the UI. Set False to post as
        though from a human-operated nick.
        """
        body = {
            "channel": channel,
            "nick": self.nick or envelope.sender,
            "content": envelope.to_relay_content(),
            "agent": agent,
        }
        resp = self._client.post(
            f"/v1/channels/{_path_segment(channel)}/messages",
            json=body, headers=self._headers()
        )
        if resp.status_code not in (200, 201):
            raise RelayError(
                f"POST /v1/channels/{channel}/messages -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        return resp.json()

    def send_text(
        self, channel: str, text: str, *, kind: str = "message", **kw: Any
    ) -> dict[str, Any]:
        """Convenience: build-and-send in one call for the common case of a
        plain message with no structured payload."""
        env = Envelope.new(kind, self.nick or "", text, **kw)
        return self.send(channel, env)

    def history(
        self, channel: str, *, limit: int = 50, envelopes_only: bool = False
    ) -> Iterator[dict[str, Any] | Envelope]:
        """Yield recent messages, newest last (matching the relay's own
        history order). With `envelopes_only=True`, silently skips messages
        that carry no awrelay envelope (ordinary chat) rather than
        returning `None` entries the caller has to filter itself.
        """
        resp = self._client.get(
            f"/v1/channels/{_path_segment(channel)}/messages",
            params={"limit": limit},
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RelayError(
                f"GET /v1/channels/{channel}/messages -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        data = resp.json()
        messages = data.get("messages", data) if isinstance(data, dict) else data
        for msg in messages:
            if not envelopes_only:
                yield msg
                continue
            env = Envelope.from_relay_content(msg.get("content", ""))
            if env is not None:
                yield env

    # ── Threads ──────────────────────────────────────────────────────────
    # AitherRelay already has real threading (channel replies AND titled
    # forum threads) — this was simply never wired into the client. Request
    # shapes match the server's `ThreadReplyRequest`/`CreateChannelThreadRequest`
    # exactly (content/nick/agent); see AitherRelay.py ~8551-8760.

    def reply_in_thread(
        self, channel: str, message_id: str, text: str, *, agent: bool = True
    ) -> dict[str, Any]:
        """Reply to `message_id`, creating its thread if it doesn't exist yet."""
        body = {"content": text, "nick": self.nick or "", "agent": agent}
        resp = self._client.post(
            f"/v1/channels/{_path_segment(channel)}/messages/{_path_segment(message_id)}/thread",
            json=body, headers=self._headers(),
        )
        if resp.status_code not in (200, 201):
            raise RelayError(
                f"POST .../messages/{message_id}/thread -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        return resp.json()

    def get_thread(self, channel: str, message_id: str) -> dict[str, Any]:
        """Every reply under `message_id`, plus the thread's metadata
        (reply_count, participants, last_reply_at)."""
        resp = self._client.get(
            f"/v1/channels/{_path_segment(channel)}/messages/{_path_segment(message_id)}/thread",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RelayError(
                f"GET .../messages/{message_id}/thread -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        return resp.json()

    def list_threads(self, channel: str) -> list[dict[str, Any]]:
        """Thread roots for a forum-mode channel — title, author, reply
        count, pin/lock state. Ordinary chat channels return an empty list
        (a reply-thread has no root entry here; use `get_thread` for those)."""
        resp = self._client.get(
            f"/v1/channels/{_path_segment(channel)}/threads",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RelayError(
                f"GET /v1/channels/{channel}/threads -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        data = resp.json()
        return data.get("threads", data) if isinstance(data, dict) else data

    def create_thread(
        self, channel: str, title: str, text: str, *, agent: bool = True
    ) -> dict[str, Any]:
        """Start a titled forum thread in a `mode="forum"` channel — a root
        message plus its ThreadInfo. Ordinary chat channels don't need this;
        `send_text` + `reply_in_thread` covers a chat-shaped thread."""
        body = {"title": title, "content": text, "nick": self.nick or "", "agent": agent}
        resp = self._client.post(
            f"/v1/channels/{_path_segment(channel)}/threads",
            json=body, headers=self._headers(),
        )
        if resp.status_code not in (200, 201):
            raise RelayError(
                f"POST /v1/channels/{channel}/threads -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        return resp.json()

    # ── Presence ─────────────────────────────────────────────────────────
    # Who is ACTUALLY connected right now, via AitherRelay's real-time
    # WebSocket connection map -- not `channels()`/member-list status, which
    # is set once at registration and never updated (permanently "online").
    # This client posts nothing to announce itself: it's a REST caller, not
    # a WebSocket participant, so "am I online" isn't a meaningful question
    # for it to ask — `presence()` answers "who else is here", which is.

    def presence(self, channel: str) -> list[dict[str, Any]]:
        """Nicks with a live WebSocket connection who are also members of
        `channel`, each as `{"nick": ..., "is_agent": ...}`."""
        resp = self._client.get(
            f"/v1/channels/{_path_segment(channel)}/presence", headers=self._headers()
        )
        if resp.status_code != 200:
            raise RelayError(
                f"GET /v1/channels/{channel}/presence -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        data = resp.json()
        return data.get("online", data) if isinstance(data, dict) else data

    # ── Reactions ────────────────────────────────────────────────────────
    # AitherRelay's reaction route is TOGGLE semantics, not add/remove: one
    # POST either adds or removes the caller's own reaction, whichever the
    # current state calls for. The result comes back inline on every message
    # a channel/thread read already returns (`Message.reactions`), so this
    # client has no separate "get reactions" call — `history()`/`get_thread()`
    # already carry it.

    def react(self, channel: str, message_id: str, emoji: str) -> None:
        """Toggle `emoji` from the caller on a message: adds it if the
        caller hasn't reacted with it yet, removes it if they have."""
        body = {"emoji": emoji, "nick": self.nick or ""}
        resp = self._client.post(
            f"/v1/channels/{_path_segment(channel)}/messages/{_path_segment(message_id)}/react",
            json=body, headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RelayError(
                f"POST .../messages/{message_id}/react -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )

    # ── Search ───────────────────────────────────────────────────────────

    def search(
        self, query: str, *, channel: str = "", workspace: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Full-text search over message content. Scope to one `channel` or
        an entire `workspace`; omit both to search every channel the caller
        can see. Empty `query` is rejected server-side (400), not silently
        treated as "match everything"."""
        if not query:
            raise RelayError("search() requires a non-empty query")
        resp = self._client.get(
            "/v1/search",
            params={"q": query, "channel": channel, "workspace": workspace, "limit": limit},
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RelayError(f"GET /v1/search -> {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("results", [])

    # ── Read state / delivery ───────────────────────────────────────────
    # "Delivery guarantees" in the REST-client sense this package can offer:
    # not an ack-per-message protocol (Relay has none), but the read-cursor
    # AitherRelay already tracks server-side — so an agent can tell what it
    # has and hasn't seen across a restart, same as a human client would.

    def mark_read(self, channel: str) -> None:
        """Advance this nick's read cursor for `channel` to now."""
        resp = self._client.post(
            f"/v1/channels/{_path_segment(channel)}/read",
            params={"nick": self.nick or ""},
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RelayError(
                f"POST /v1/channels/{channel}/read -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )

    def unread_counts(self) -> dict[str, Any]:
        """Unread message counts per channel this nick belongs to, since its
        last `mark_read` cursor."""
        resp = self._client.get(
            "/v1/unread", params={"nick": self.nick or ""}, headers=self._headers()
        )
        if resp.status_code != 200:
            raise RelayError(f"GET /v1/unread -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ── Pins ─────────────────────────────────────────────────────────────
    # Moderator-only server-side (403 otherwise) — this client does not
    # duplicate that check; it surfaces whatever AitherRelay decides via
    # RelayError, same as every other write here.

    def pin(self, channel: str, message_id: str) -> None:
        resp = self._client.post(
            f"/v1/channels/{_path_segment(channel)}/pin/{_path_segment(message_id)}",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RelayError(
                f"POST .../pin/{message_id} -> {resp.status_code}: {resp.text[:200]}"
            )

    def unpin(self, channel: str, message_id: str) -> None:
        resp = self._client.delete(
            f"/v1/channels/{_path_segment(channel)}/pin/{_path_segment(message_id)}",
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise RelayError(
                f"DELETE .../pin/{message_id} -> {resp.status_code}: {resp.text[:200]}"
            )

    def pinned(self, channel: str) -> list[dict[str, Any]]:
        resp = self._client.get(
            f"/v1/channels/{_path_segment(channel)}/pins", headers=self._headers()
        )
        if resp.status_code != 200:
            raise RelayError(
                f"GET /v1/channels/{channel}/pins -> {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        return data.get("pinned", data) if isinstance(data, dict) else data
