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

    sub.add_parser("mcp", help="serve over MCP stdio for a coding agent")

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "mcp":
        from awrelay.mcp_server import main as mcp_main
        return mcp_main()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
