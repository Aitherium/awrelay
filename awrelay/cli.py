"""awrelay CLI — send/watch/list against an AitherRelay-shaped server.

    awrelay send '#agent-lounge' "found a race condition" --kind finding
    awrelay history '#agent-lounge' --envelopes-only
    awrelay channels

Connection is read from flags or env vars (AWRELAY_URL, AWRELAY_TOKEN,
AWRELAY_NICK) — flags win. No config file: a messaging CLI that silently
reads a stale saved endpoint is worse than one that asks every time.

Exit codes: 0 success, 1 the relay refused/was unreachable (RelayError),
2 the command could not run at all (bad args, missing env). Matches the
awgit/awgraph convention: a script can tell "the relay said no" from
"this invocation was wrong" from "nothing matched".
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from awrelay.client import RelayClient, RelayError
from awrelay.envelope import Envelope


def _client_from_args(args: argparse.Namespace) -> RelayClient:
    url = args.url or os.environ.get("AWRELAY_URL")
    if not url:
        print("awrelay: no relay URL — pass --url or set AWRELAY_URL", file=sys.stderr)
        raise SystemExit(2)
    token = args.token or os.environ.get("AWRELAY_TOKEN")
    nick = args.nick or os.environ.get("AWRELAY_NICK")
    return RelayClient(url, token=token, nick=nick)


def _cmd_send(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    payload = json.loads(args.payload) if args.payload else {}
    try:
        env = Envelope.new(args.kind, client.nick or "", args.text, payload=payload)
        result = client.send(args.channel, env)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        print(f"sent to {args.channel}: {args.text}")
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        items = list(
            client.history(args.channel, limit=args.limit, envelopes_only=args.envelopes_only)
        )
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(
            [i.__dict__ if isinstance(i, Envelope) else i for i in items], default=str
        ))
        return 0
    if not items:
        print("(no messages)")
        return 0
    for item in items:
        if isinstance(item, Envelope):
            corr = f" corr={item.correlation_id}" if item.correlation_id else ""
            print(f"[{item.kind.value}] {item.sender}: {item.text}{corr}")
        else:
            print(f"{item.get('nick', '?')}: {item.get('content', '')}")
    return 0


def _cmd_channels(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        chans = client.channels()
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(chans))
        return 0
    if not chans:
        print("(no channels visible)")
        return 0
    for c in chans:
        name = c.get("name", c) if isinstance(c, dict) else c
        print(name)
    return 0


def _cmd_thread_reply(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        result = client.reply_in_thread(args.channel, args.message_id, args.text)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result) if args.json else f"replied in thread {args.message_id}")
    return 0


def _cmd_thread_get(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        result = client.get_thread(args.channel, args.message_id)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result))
        return 0
    replies = result.get("replies", [])
    if not replies:
        print("(no replies)")
        return 0
    for r in replies:
        print(f"{r.get('nick', '?')}: {r.get('content', '')}")
    return 0


def _cmd_threads(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        threads = client.list_threads(args.channel)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(threads))
        return 0
    if not threads:
        print("(no threads)")
        return 0
    for t in threads:
        print(f"{t.get('title', '(untitled)')} — {t.get('reply_count', 0)} replies")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        results = client.search(
            args.query, channel=args.channel or "", workspace=args.workspace or ""
        )
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(results))
        return 0
    if not results:
        print("(no matches)")
        return 0
    for r in results:
        print(f"{r.get('nick', '?')} [{r.get('channel', '?')}]: {r.get('content', '')}")
    return 0


def _cmd_unread(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        counts = client.unread_counts()
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(counts))
    return 0


def _cmd_mark_read(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        client.mark_read(args.channel)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    print(f"marked {args.channel} read")
    return 0


def _cmd_presence(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        online = client.presence(args.channel)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(online))
        return 0
    if not online:
        print("(nobody currently connected)")
        return 0
    for u in online:
        print(u.get("nick", "?"))
    return 0


def _cmd_react(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        client.react(args.channel, args.message_id, args.emoji)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    print(f"toggled {args.emoji} on {args.message_id}")
    return 0


def _cmd_pin(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        if args.unpin:
            client.unpin(args.channel, args.message_id)
            print(f"unpinned {args.message_id}")
        else:
            client.pin(args.channel, args.message_id)
            print(f"pinned {args.message_id}")
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_pins(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    try:
        pinned = client.pinned(args.channel)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(pinned))
        return 0
    if not pinned:
        print("(no pinned messages)")
        return 0
    for p in pinned:
        print(f"{p.get('nick', '?')}: {p.get('content', '')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="awrelay", description=__doc__)
    ap.add_argument("--url", help="relay server origin (or AWRELAY_URL)")
    ap.add_argument("--token", help="bearer token (or AWRELAY_TOKEN)")
    ap.add_argument("--nick", help="this agent's nick (or AWRELAY_NICK)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")

    sub = ap.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", help="post a message to a channel")
    p_send.add_argument("channel")
    p_send.add_argument("text")
    p_send.add_argument("--kind", default="message",
                         choices=["message", "finding", "alert", "request", "steer", "ack"])
    p_send.add_argument("--payload", help="JSON object for the structured payload")
    p_send.set_defaults(func=_cmd_send)

    p_hist = sub.add_parser("history", help="show recent messages in a channel")
    p_hist.add_argument("channel")
    p_hist.add_argument("--limit", type=int, default=50)
    p_hist.add_argument("--envelopes-only", action="store_true",
                         help="skip messages with no awrelay envelope")
    p_hist.set_defaults(func=_cmd_history)

    p_chan = sub.add_parser("channels", help="list visible channels")
    p_chan.set_defaults(func=_cmd_channels)

    p_treply = sub.add_parser(
        "thread-reply", help="reply to a message, creating its thread if needed"
    )
    p_treply.add_argument("channel")
    p_treply.add_argument("message_id")
    p_treply.add_argument("text")
    p_treply.set_defaults(func=_cmd_thread_reply)

    p_tget = sub.add_parser("thread", help="show every reply under a message")
    p_tget.add_argument("channel")
    p_tget.add_argument("message_id")
    p_tget.set_defaults(func=_cmd_thread_get)

    p_threads = sub.add_parser("threads", help="list forum thread roots in a channel")
    p_threads.add_argument("channel")
    p_threads.set_defaults(func=_cmd_threads)

    p_search = sub.add_parser("search", help="full-text search over message content")
    p_search.add_argument("query")
    p_search.add_argument("--channel", help="scope to one channel")
    p_search.add_argument("--workspace", help="scope to one workspace")
    p_search.set_defaults(func=_cmd_search)

    p_unread = sub.add_parser("unread", help="unread counts per channel")
    p_unread.set_defaults(func=_cmd_unread)

    p_mark = sub.add_parser("mark-read", help="advance this nick's read cursor for a channel")
    p_mark.add_argument("channel")
    p_mark.set_defaults(func=_cmd_mark_read)

    p_presence = sub.add_parser("presence", help="who is actually connected right now in a channel")
    p_presence.add_argument("channel")
    p_presence.set_defaults(func=_cmd_presence)

    p_react = sub.add_parser("react", help="toggle an emoji reaction on a message")
    p_react.add_argument("channel")
    p_react.add_argument("message_id")
    p_react.add_argument("emoji")
    p_react.set_defaults(func=_cmd_react)

    p_pin = sub.add_parser("pin", help="pin (or --unpin) a message — moderator-only server-side")
    p_pin.add_argument("channel")
    p_pin.add_argument("message_id")
    p_pin.add_argument("--unpin", action="store_true")
    p_pin.set_defaults(func=_cmd_pin)

    p_pins = sub.add_parser("pins", help="list pinned messages in a channel")
    p_pins.add_argument("channel")
    p_pins.set_defaults(func=_cmd_pins)

    sub.add_parser("mcp", help="serve over MCP stdio for a coding agent")

    return ap


def _force_utf8_stdio() -> None:
    """Make the console tolerate the emoji this relay actually carries.

    Windows consoles default to cp1252, and `print()` of any character outside
    it raises UnicodeEncodeError -- which aborts the command with a traceback
    AFTER the network round-trip has already succeeded. Measured 2026-08-19:
    `awrelay history '#playground'` fetched the channel fine and then died on
    a moon emoji, so a working relay read like a broken one.

    That is not an exotic input here: the relay's own announcements are full of
    them (announce lines lead with a play triangle, a trophy, a moon, a brain),
    so on Windows `history` was unusable on any channel a service posts to.

    errors="replace" rather than a narrower encoding: a message that cannot be
    rendered should show a replacement glyph, never cost the reader the other
    twenty messages in the buffer.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:      # not a TextIOWrapper (piped/captured)
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # A stream that refuses reconfiguration is not worth failing the
            # command over; the print below may still succeed.
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "mcp":
        from awrelay.mcp_server import main as mcp_main
        return mcp_main()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
