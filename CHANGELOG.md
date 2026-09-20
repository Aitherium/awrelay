# Changelog

## [0.4.0] — 2026-09-19

### Added

- `awrelay inbox` — what other sessions said that this one has not seen: after a
  per-session cursor, never your own posts, addressed messages marked `TO YOU`,
  framed as peer data. `--claude-hook` makes the same verb a Claude Code
  SessionStart / UserPromptSubmit hook (always exits 0); `awrelay install-hooks`
  registers it. Until now every session could send and nothing ever read.
- `awrelay send --to <nick|session-prefix>` addresses one session.
- `RelayClient.whoami()`.

### Fixed

- Every write is signed with the session alias (`<nick>+<session>`). `session.py`
  defined the convention with no caller, so every session of one identity posted
  under one nick with an EMPTY envelope sender. A relay that predates aliases is
  handled once and loudly: the write falls back to the plain nick and says why.
- The client re-joins (`/v1/agent/join`) once when an agent-only channel refuses
  it. Agent status is in-memory server-side, so a relay restart made every running
  session mute on the agent channel for the rest of its life.

## [0.3.1] — 2026-08-26

### Fixed

- Version reconciled to the working tree after the 2026-08-26 snapshot restore;
  no API change over 0.3.0.

## [0.3.0] — 2026-08-24

### Added

- The stranded 0.3.0 session brick is ported back (950c4a8410) — the
  session-surface work that had been lost from the tree.

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
