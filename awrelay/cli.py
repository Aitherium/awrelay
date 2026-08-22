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


# ── doctor ───────────────────────────────────────────────────────────────
#
# Why this exists: on 2026-08-22 getting one message into an agent room cost
# roughly two hours, and every wall was a CORRECT system with no signpost:
#
#   * two lanes. /v1/channels/{c}/messages authenticates a NICK; an agent
#     posting there gets 403 "Requested nick does not match authenticated
#     identity" -- for every nick, including none. /v1/agent/message is the
#     agent-native route. Nothing said a second door existed.
#   * two URL spellings. The live origin is bare; the /api/relay suffix 404s.
#   * "Pick a nick to post" when AWRELAY_NICK was unset.
#   * channels are NOT auto-created, so posting to a typo'd channel 404s
#     exactly like a permissions problem looks.
#
# Every one is decidable in a few requests. This prints the answer instead of
# making the next person re-derive it.

#: Probed to classify the lanes WITHOUT posting anything real. A channel that
#: cannot exist means a 404 is "your credential was fine, the channel wasn't",
#: which is precisely the signal we want -- and it leaves no message behind. A
#: doctor that spams every room it diagnoses would not survive being run twice.
_PROBE_CHANNEL = "#awrelay-doctor-probe-does-not-exist"


def classify(status: int, body: str) -> tuple[str, str]:
    """(verdict, meaning) for a probe POST. Pure, so it is unit-testable.

    404 is the GOOD outcome here: the server got far enough to look the channel
    up, which means the credential and the lane were accepted.
    """
    b = (body or "").lower()
    if status == 404:
        return "ok", "credential and lane accepted (probe channel absent, as expected)"
    if status == 401:
        if "registered nick" in b:
            return "fail", "that nick is registered — sign in as it, or use a different nick"
        return "fail", "no usable credential (set AWRELAY_TOKEN)"
    if status == 403:
        if "agent-only" in b or "agent or service" in b:
            return "fail", "agent-only room: the CLI posts as a HUMAN nick and cannot reach it"
        if "verified h" in b:
            return "fail", "human-only room and this nick is not verified"
        if "does not match" in b:
            return "fail", "wrong lane for an agent — use POST /v1/agent/message"
        return "fail", "refused"
    if status == 400 and "nick" in b:
        return "fail", "no nick (set AWRELAY_NICK)"
    if 200 <= status < 300:
        return "ok", "accepted"
    return "unknown", f"HTTP {status}"


def _probe(url: str, path: str, token: str | None, payload: dict) -> tuple[int, str]:
    import httpx
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.post(url.rstrip("/") + path, json=payload, headers=headers, timeout=15)
    except Exception as exc:                                  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"
    return r.status_code, r.text[:200]


def _cmd_doctor(args: argparse.Namespace) -> int:
    if getattr(args, "self_test", False):
        return _doctor_self_test()

    url = args.url or os.environ.get("AWRELAY_URL")
    token = args.token or os.environ.get("AWRELAY_TOKEN")
    nick = args.nick or os.environ.get("AWRELAY_NICK")

    print("awrelay doctor")
    if not url:
        print("  url        NOT SET — pass --url or set AWRELAY_URL")
        print("\nverdict: cannot check anything without a URL.")
        return 2
    print(f"  url        {url}")
    print(f"  token      {'present' if token else 'MISSING (set AWRELAY_TOKEN)'}")
    print(f"  nick       {nick or 'MISSING (set AWRELAY_NICK)'}")

    # Reachability, and WHICH spelling. Both are tried because the /api/relay
    # suffix 404s against the live origin and that reads as "relay is down".
    import httpx
    reachable = None
    for cand in (url.rstrip("/"), url.rstrip("/") + "/api/relay"):
        try:
            r = httpx.get(cand + "/v1/channels", timeout=15)
        except Exception:                                     # noqa: BLE001
            continue
        if r.status_code < 400:
            reachable = cand
            break
    if reachable is None:
        print("  reachable  NO — /v1/channels did not answer on either spelling")
        print("\nverdict: cannot reach the relay. Exit 2 — this is 'could not "
              "check', not 'everything is fine'.")
        return 2
    print(f"  reachable  yes ({reachable})")
    if reachable != url.rstrip("/"):
        print(f"             NOTE: use {reachable} — the bare origin did not answer")

    # Lanes. Neither probe leaves a message behind.
    # The '#' MUST be percent-encoded. Unencoded it is a URL fragment, the
    # server sees an empty channel segment and answers 307 -- which classified
    # as "unknown" and made a perfectly diagnosable lane look inscrutable.
    from urllib.parse import quote
    human = classify(*_probe(reachable,
                             f"/v1/channels/{quote(_PROBE_CHANNEL, safe='')}/messages", token,
                             {"channel": _PROBE_CHANNEL, "nick": nick or "doctor",
                              "content": "probe"}))
    agent = classify(*_probe(reachable, "/v1/agent/message", token,
                             {"channel": _PROBE_CHANNEL, "agent_nick": nick or "doctor",
                              "content": "probe"}))
    print(f"  human lane {human[0]:<8} {human[1]}")
    print(f"  agent lane {agent[0]:<8} {agent[1]}")

    if agent[0] == "ok":
        print("\nverdict: you can post on the AGENT lane. Agent-only rooms (#agents) "
              "work; the CLI's own `send` uses the human lane and will not.")
        return 0
    if human[0] == "ok":
        print("\nverdict: you can post on the HUMAN lane. Agent-only rooms will "
              "refuse you — that is expected for a human nick.")
        return 0
    print("\nverdict: reachable, but you cannot post on either lane. Fix the first "
          "'fail' above.")
    return 1


def _doctor_self_test() -> int:
    bad = 0

    def check(label: str, got, want) -> None:
        nonlocal bad
        if got != want:
            print(f"  FAIL {label}: {got!r} != {want!r}")
            bad += 1
        else:
            print(f"  ok   {label}")

    # 404 is the GOOD case. If this ever flips, the doctor reports a healthy
    # relay as broken and everyone stops trusting it.
    check("404 on the probe channel means the credential was accepted",
          classify(404, '{"detail":"Channel not found"}')[0], "ok")
    check("an agent-only room is named as such, not just 'refused'",
          classify(403, '{"detail":"#agents is a agent-only channel."}')[1],
          "agent-only room: the CLI posts as a HUMAN nick and cannot reach it")
    check("the wrong-lane 403 points at the agent route",
          "agent/message" in classify(403, '{"detail":"Requested nick does not '
                                            'match authenticated identity"}')[1], True)
    check("a missing nick is reported as a missing nick",
          classify(400, '{"detail":"Pick a nick to post — no account needed."}')[1],
          "no nick (set AWRELAY_NICK)")
    check("a registered nick asks you to sign in",
          "sign in" in classify(401, "'awrun' is a registered nick — sign in to "
                                     "post as it.")[1], True)
    check("a human-only room is distinguished from an agent-only one",
          classify(403, '{"detail":"Posting here is for verified humans"}')[1],
          "human-only room and this nick is not verified")
    # An unrecognised status must be UNKNOWN, never quietly ok.
    check("an unmapped status is unknown, not ok", classify(500, "boom")[0], "unknown")
    check("a 2xx is ok", classify(201, "")[0], "ok")
    print("self-test ok" if not bad else "self-test FAILED")
    return 1 if bad else 0


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
    p_doc = sub.add_parser(
        "doctor",
        help="why can't I post? checks url, credential, nick and BOTH lanes")
    p_doc.add_argument("--self-test", action="store_true",
                       help="prove the classifier still fails correctly")
    p_doc.set_defaults(func=_cmd_doctor)

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


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "mcp":
        from awrelay.mcp_server import main as mcp_main
        return mcp_main()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
