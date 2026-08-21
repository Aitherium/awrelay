"""An MCP server for awrelay — give a coding agent a channel to talk in.

    pip install "awrelay[mcp]"

then point a client at `awrelay mcp`:

    {"mcpServers": {"awrelay": {"command": "awrelay",
                                 "args": ["mcp"],
                                 "env": {"AWRELAY_URL": "...", "AWRELAY_TOKEN": "..."}}}}

WHY THIS EXISTS
---------------
An agent that finds something worth telling another agent — a bug, a blocked
task, a request for a decision — has nowhere to put that except its own
transcript, which no other agent reads. This gives it a channel: post a
structured Envelope into an AitherRelay-shaped chat room, and read one back.

DESIGN NOTES THAT MATTER
-------------------------
**Connection is fixed at server start**, from AWRELAY_URL / AWRELAY_TOKEN /
AWRELAY_NICK in the environment — not a per-call argument. An MCP tool
argument is caller-suppliable, and letting a tool call redirect which server
gets a bearer token is the same shape as accepting a caller-supplied identity
for an authz decision (security-review-patterns.md #2): here the "decision"
is which relay receives the token, so the environment — configured by whoever
set up the MCP client entry, not by the model — is the only place that gets
to make it.

**No fallback, no offline mode**, matching client.py: a channel the agent
cannot reach is reported as unreachable, not silently degraded into "sent"
with nothing behind it.

SDK VERSION
-----------
Written against the `mcp` 2.x `MCPServer` API, matching awgraph's mcp_server.py
— see that module's docstring for why the version is pinned rather than
detected: 1.x and 2.x expose incompatible surfaces, and code passing tests
against a locally installed 1.x has failed on a clean install that resolved
2.0.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

_INSTALL_HINT = (
    "The MCP server needs the `mcp` package: pip install \"awrelay[mcp]\". "
    "Raised rather than degraded, because an MCP server that starts and "
    "serves no tools looks to the client exactly like a server with nothing "
    "to offer."
)


def _client_from_env():
    from awrelay.client import RelayClient

    url = os.environ.get("AWRELAY_URL")
    if not url:
        raise RuntimeError(
            "AWRELAY_URL is not set. The MCP client config must set it "
            "(and AWRELAY_TOKEN, AWRELAY_NICK) in the server's env block — "
            "not passed per tool call. See this module's docstring."
        )
    return RelayClient(
        url,
        token=os.environ.get("AWRELAY_TOKEN"),
        nick=os.environ.get("AWRELAY_NICK"),
    )


def _a2a_bridge_from_env():
    from awrelay.a2a_bridge import A2ABridge

    url = os.environ.get("AWRELAY_A2A_URL")
    if not url:
        raise RuntimeError(
            "AWRELAY_A2A_URL is not set. Separate from AWRELAY_URL — this is "
            "AitherA2A's own origin, e.g. http://a2a-host:8766. Never an "
            "internal-key credential; this bridge never carries one, by design."
        )
    return A2ABridge(url)


def build_server():
    """Construct the MCP server. Raises ImportError if `mcp` is absent."""
    try:
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - exercised by a bare install
        raise ImportError(_INSTALL_HINT) from exc

    from awrelay.client import RelayError
    from awrelay.envelope import Envelope

    server = MCPServer(
        name="awrelay",
        instructions=(
            "Agent-to-agent messaging over an AitherRelay-shaped chat server. "
            "relay_send posts a structured message (a finding, an alert, a "
            "plain message) into a channel; relay_history reads recent "
            "messages back, decoding any that carry a structured envelope."
        ),
    )

    @server.tool(
        name="relay_channels",
        description="List channels this agent can see (public + its own private ones).",
    )
    async def relay_channels() -> str:
        client = _client_from_env()
        try:
            return json.dumps(client.channels())
        except RelayError as exc:
            return f"Could not list channels: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_send",
        description=(
            "Post a message into a channel. `kind` marks intent for other "
            "agents reading the channel (finding, alert, request, steer, "
            "ack, or plain message)."
        ),
    )
    async def relay_send(
        channel: str, text: str, kind: str = "message",
        payload: str | None = None, correlation_id: str | None = None,
    ) -> str:
        """
        Args:
            channel: Channel name, e.g. "#agent-lounge".
            text: The message body.
            kind: One of message, finding, alert, request, steer, ack.
            payload: Optional JSON object string with structured data.
            correlation_id: Optional id linking this to an earlier message
                (e.g. answering a "request").
        """
        client = _client_from_env()
        try:
            parsed_payload = json.loads(payload) if payload else None
            env = Envelope.new(
                kind, client.nick or "mcp-agent", text,
                payload=parsed_payload, correlation_id=correlation_id,
            )
            result = client.send(channel, env)
            return json.dumps(result)
        except RelayError as exc:
            return f"Send failed: {exc}"
        except (ValueError, json.JSONDecodeError) as exc:
            return f"Bad arguments: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_history",
        description=(
            "Read recent messages from a channel. With envelopes_only, "
            "skips plain chat and returns only structured awrelay messages."
        ),
    )
    async def relay_history(
        channel: str, limit: int = 50, envelopes_only: bool = False,
    ) -> str:
        client = _client_from_env()
        try:
            items = list(client.history(channel, limit=limit, envelopes_only=envelopes_only))
        except RelayError as exc:
            return f"History fetch failed: {exc}"
        finally:
            client.close()
        out: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, Envelope):
                out.append({
                    "kind": item.kind.value, "sender": item.sender,
                    "text": item.text, "payload": item.payload,
                    "correlation_id": item.correlation_id,
                })
            else:
                out.append(item)
        return json.dumps(out)

    @server.tool(
        name="relay_reply_in_thread",
        description="Reply to a specific message, creating its thread if needed.",
    )
    async def relay_reply_in_thread(channel: str, message_id: str, text: str) -> str:
        client = _client_from_env()
        try:
            return json.dumps(client.reply_in_thread(channel, message_id, text))
        except RelayError as exc:
            return f"Thread reply failed: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_get_thread",
        description=(
            "Read every reply under a message, plus thread metadata "
            "(reply count, participants)."
        ),
    )
    async def relay_get_thread(channel: str, message_id: str) -> str:
        client = _client_from_env()
        try:
            return json.dumps(client.get_thread(channel, message_id))
        except RelayError as exc:
            return f"Thread fetch failed: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_threads",
        description=(
            "List thread roots in a forum-mode channel (title, author, reply "
            "count, pin/lock state). Empty for an ordinary chat channel."
        ),
    )
    async def relay_threads(channel: str) -> str:
        client = _client_from_env()
        try:
            return json.dumps(client.list_threads(channel))
        except RelayError as exc:
            return f"Thread list failed: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_search",
        description=(
            "Full-text search over message content, optionally scoped to "
            "one channel or workspace."
        ),
    )
    async def relay_search(
        query: str, channel: str = "", workspace: str = "", limit: int = 50,
    ) -> str:
        client = _client_from_env()
        try:
            return json.dumps(
                client.search(query, channel=channel, workspace=workspace, limit=limit)
            )
        except RelayError as exc:
            return f"Search failed: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_unread",
        description="Unread message counts per channel this agent's nick belongs to.",
    )
    async def relay_unread() -> str:
        client = _client_from_env()
        try:
            return json.dumps(client.unread_counts())
        except RelayError as exc:
            return f"Unread fetch failed: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_a2a_call",
        description=(
            "Invoke a skill on an AitherA2A-registered service. First call "
            "for a new (service, skill) pair returns a pending-approval "
            "notice naming access_request_id -- get a human to approve it "
            "via AitherA2A's own admin surface, then retry passing the "
            "returned grant_token as grant_token here. Never sends an "
            "internal-fleet credential; this is a genuinely external-shaped "
            "caller."
        ),
    )
    async def relay_a2a_call(
        service: str, skill: str, params: str | None = None, grant_token: str | None = None,
    ) -> str:
        from awrelay.a2a_bridge import A2AApprovalPendingError, A2AError

        bridge = _a2a_bridge_from_env()
        try:
            parsed = json.loads(params) if params else None
            result = bridge.call(service, skill, parsed, grant_token=grant_token)
            return json.dumps(result)
        except A2AApprovalPendingError as exc:
            return json.dumps({
                "status": "approval_pending",
                "resource": exc.resource,
                "access_request_id": exc.access_request_id,
                "how_to_proceed": exc.how_to_proceed,
            })
        except A2AError as exc:
            return f"A2A call failed: {exc}"
        except (ValueError, json.JSONDecodeError) as exc:
            return f"Bad arguments: {exc}"
        finally:
            bridge.close()

    @server.tool(
        name="relay_a2a_delegate",
        description=(
            "Delegate a task to another A2A-registered agent. Same "
            "pending-approval / grant_token flow as relay_a2a_call."
        ),
    )
    async def relay_a2a_delegate(
        to_agent: str, message: str, from_agent: str = "awrelay-bridge",
        session_id: str | None = None, grant_token: str | None = None,
    ) -> str:
        from awrelay.a2a_bridge import A2AApprovalPendingError, A2AError

        bridge = _a2a_bridge_from_env()
        try:
            result = bridge.delegate(
                to_agent, message, from_agent=from_agent,
                session_id=session_id, grant_token=grant_token,
            )
            return json.dumps(result)
        except A2AApprovalPendingError as exc:
            return json.dumps({
                "status": "approval_pending",
                "resource": exc.resource,
                "access_request_id": exc.access_request_id,
                "how_to_proceed": exc.how_to_proceed,
            })
        except A2AError as exc:
            return f"A2A delegate failed: {exc}"
        finally:
            bridge.close()

    @server.tool(
        name="relay_presence",
        description=(
            "Nicks with a live connection who are also members of a "
            "channel (real-time, not membership status)."
        ),
    )
    async def relay_presence(channel: str) -> str:
        client = _client_from_env()
        try:
            return json.dumps(client.presence(channel))
        except RelayError as exc:
            return f"Presence fetch failed: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_react",
        description=(
            "Toggle an emoji reaction on a message: adds it if the agent "
            "hasn't reacted with it yet, removes it if they have."
        ),
    )
    async def relay_react(channel: str, message_id: str, emoji: str) -> str:
        client = _client_from_env()
        try:
            client.react(channel, message_id, emoji)
            return json.dumps(
                {"ok": True, "channel": channel, "message_id": message_id, "emoji": emoji}
            )
        except RelayError as exc:
            return f"React failed: {exc}"
        finally:
            client.close()

    @server.tool(
        name="relay_mark_read",
        description="Advance this agent's read cursor for a channel to now.",
    )
    async def relay_mark_read(channel: str) -> str:
        client = _client_from_env()
        try:
            client.mark_read(channel)
            return json.dumps({"ok": True, "channel": channel})
        except RelayError as exc:
            return f"Mark-read failed: {exc}"
        finally:
            client.close()

    return server


def main(argv: list[str] | None = None) -> int:
    """Serve over stdio. Blocks until the client disconnects."""
    import asyncio

    try:
        server = build_server()
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        asyncio.run(server.run_stdio_async())
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
