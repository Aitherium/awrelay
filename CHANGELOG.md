# Changelog

## [0.2.0] — 2026-08-19

Full-featured build, replacing the previous 3-tool thin client:

- **Threading** — `reply_in_thread`, `get_thread`, `list_threads`, `create_thread`,
  wiring the REST client to AitherRelay routes that already existed server-side.
- **Search** — `search(query, channel=, workspace=)`.
- **Read-state** — `mark_read`, `unread_counts`.
- **Pins** — `pin`, `unpin`, `pinned`.
- **Reactions** — `react`, wired to AitherRelay's existing toggle endpoint.
- **Presence** — `presence(channel)`, backed by a new AitherRelay route
  (`GET /v1/channels/{channel}/presence`) reporting who is actually connected,
  not just who is a member.
- **A2A bridge** (`awrelay.a2a_bridge`) — bridges an `Envelope` to AitherA2A's
  `/call` and `/delegate`, built as an external-shaped caller that never holds
  the fleet's internal key, so it is scoped by the same per-skill grant flow
  every other external peer goes through.
- MCP server surface grew from 3 tools to 15; CLI grew matching subcommands.

## [0.1.0] — initial release

REST client (`send_text`, `read_recent`, `list_channels`), `Envelope`, CLI, and a
3-tool MCP server (`relay_channels`, `relay_send`, `relay_history`).
