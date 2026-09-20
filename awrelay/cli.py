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
import time

import httpx

from awrelay.client import RelayClient, RelayError
from awrelay.envelope import Envelope


def _client_from_args(args: argparse.Namespace) -> RelayClient:
    url = args.url or os.environ.get("AWRELAY_URL")
    if not url:
        print("awrelay: no relay URL — pass --url or set AWRELAY_URL", file=sys.stderr)
        raise SystemExit(2)
    token = args.token or os.environ.get("AWRELAY_TOKEN") or _session_bearer()
    if not token:
        # NO ANONYMOUS PATH. A first-party session either presents its identity or does not
        # post. Degrading to a walk-in with an invented nick is the silent fallback the owner
        # forbids -- and it is exactly how a maintenance notice would land as an untrusted
        # stranger's message, or not land at all.
        print("awrelay: no identity. Pass --token / set AWRELAY_TOKEN, or mint the session bearer "
              f"this CLI reads by default ({_BEARER_FILE}):\n"
              "  python AitherOS/dev/tools/mint_session_bearer.py", file=sys.stderr)
        raise SystemExit(2)
    # With a bearer the relay binds the nick to the authenticated identity and answers 403
    # "Requested nick does not match authenticated identity" to any other, so a nick is only
    # ever an explicit override.
    nick = args.nick or os.environ.get("AWRELAY_NICK")
    client = RelayClient(url, token=token, nick=nick)
    # EVERY SESSION SIGNS ITS OWN NAME. Measured 2026-09-19: 67 of 129 #agents messages
    # were from "david" with an EMPTY envelope sender -- twenty concurrent sessions, one
    # name, so nobody could tell who said what, address a reply, or skip their own posts.
    # `session.py` had defined `<nick>+<session>` for four weeks with zero callers. With no
    # explicit nick, ask the relay who this bearer is (cached a day) and sign as its alias.
    client.identity_nick = ""
    client.alias_derived = False
    if not nick and getattr(args, "command", "") not in _NO_IDENTITY_COMMANDS:
        from awrelay.inbox import resolve_identity
        identity, mine = resolve_identity(client, token,
                                          session_id=getattr(args, "session_id", "") or "")
        client.identity_nick = identity
        if mine:
            client.nick = mine
            client.alias_derived = mine != identity
    return client


#: Commands that never write and never need to know who is asking.
_NO_IDENTITY_COMMANDS = frozenset({"channels", "history", "search", "thread", "threads", "pins",
                                   "presence", "install-hooks"})


def _retry_without_alias(client: RelayClient, exc: RelayError) -> bool:
    """A relay that predates session aliases answers 403 to `<nick>+<session>`. That is a
    SERVER gap, not a reason to lose the message: fall back to the plain identity nick
    ONCE and say so on stderr -- loudly, because the fallback is exactly the
    indistinguishable state the alias exists to end (the server half of the convention was
    lost from develop for four weeks and nothing reported it)."""
    if not getattr(client, "alias_derived", False):
        return False
    # Two shapes of the same server gap, measured on two different days: a relay
    # with the alias half answers 403 "does not match authenticated identity" for
    # an alias it cannot map; a relay WITHOUT it never gets that far -- its nick
    # validator 400s the "+" itself ("Nick must be 2-32 chars: letters, numbers,
    # _ - . only", 2026-09-20 after a peer's restart shipped an older image).
    text = str(exc)
    if ("does not match authenticated identity" not in text
            and "Nick must be" not in text):
        return False
    print(f"awrelay: this relay refused the session alias {client.nick!r}; posting as "
          f"{client.identity_nick!r}. The server is missing session-alias support "
          "(AitherRelay._nick_permitted) -- peers cannot tell this session apart.",
          file=sys.stderr)
    client.nick = client.identity_nick
    client.alias_derived = False
    return True


# THE SESSION BEARER IS THE IDENTITY. Measured 2026-09-19 08:50, announcing a maintenance
# window from a Claude Code session: every channel refused ("Pick a nick to post" / "Verify
# identity") because nothing set AWRELAY_TOKEN and the CLI sent no bearer -- the relay
# correctly saw an anonymous caller, and the door fix that exempts members from the #agents
# knock (config/doors.yaml `applies_to`) cannot help a caller that never says who it is. The
# same bearer the MCP stdio bridge reads on every reconnect sits at ~/.aither/session-bearer
# (root CLAUDE.md), so the CLI reads it too. Not a fallback: it is the credential.
_BEARER_FILE = os.path.join(os.path.expanduser("~"), ".aither", "session-bearer")


def _session_bearer() -> str | None:
    try:
        with open(_BEARER_FILE, encoding="utf-8") as fh:
            tok = fh.read().strip()
        return tok or None
    except OSError:
        return None


def _cmd_send(args: argparse.Namespace) -> int:
    client = _client_from_args(args)
    payload = json.loads(args.payload) if args.payload else {}
    if args.to:
        payload = dict(payload, to=[t.strip() for t in args.to if t.strip()])
    try:
        try:
            env = Envelope.new(args.kind, client.nick or "", args.text, payload=payload)
            result = client.send(args.channel, env)
        except RelayError as exc:
            if not _retry_without_alias(client, exc):
                raise
            env = Envelope.new(args.kind, client.nick or "", args.text, payload=payload)
            result = client.send(args.channel, env)
    except RelayError as exc:
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    except httpx.TransportError as exc:
        # Nothing answered. A refusal is an answer and stays an error above; a
        # relay that is DOWN must not cost the message -- it is queued on disk and
        # flushed at the head of the next inbox read (every prompt, every tool
        # call). Exit 0: the send WILL happen, and a script chaining on it should
        # not treat an outage as its own failure.
        from awrelay import outbox

        env = Envelope.new(args.kind, client.nick or "", args.text, payload=payload)
        if outbox.enqueue(client.nick or "", args.channel, env):
            print(f"awrelay: the relay did not answer ({type(exc).__name__}); QUEUED for "
                  f"{args.channel} -- flushes at the next prompt or tool call", file=sys.stderr)
            return 0
        print(f"awrelay: the relay did not answer ({type(exc).__name__}) and the outbox could "
              f"not be written -- this message is LOST", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        who = f" as {client.nick}" if client.nick else ""
        print(f"sent to {args.channel}{who}: {args.text}")
    return 0


def _flush_outbox(client, me: str, hook_event: str) -> None:
    """Best-effort: a queued send lands as soon as the relay answers. Never raises --
    the inbox read it precedes must still happen."""
    from awrelay import outbox

    # Two queues, not one. A send during an outage that ALSO had a cold identity
    # cache could not learn its nick (the relay is what answers whoami), so it
    # queued under the empty key -- and a later flush keyed on the resolved nick
    # would walk past it forever. Measured while building ITD005: the very first
    # message after a fresh box comes up is the one most likely to be in that
    # state, and it is the one a peer is waiting on.
    queues = [me] + ([""] if me and outbox.pending("") else [])
    if not any(outbox.pending(q) for q in queues):
        return
    def _send(ch, env, agent):
        # The same alias fallback the live send path has: a relay missing the
        # alias half must not turn a queued line into an archived "refusal".
        try:
            return client.send(ch, env, agent=agent)
        except RelayError as exc:
            if not _retry_without_alias(client, exc):
                raise
            return client.send(ch, env, agent=agent)

    def _send_as_me(ch, env, agent):
        # An orphaned row carries no sender; sign it with the nick we now know,
        # so it lands attributable instead of as another nameless "david".
        if not env.sender:
            env.sender = me
        return _send(ch, env, agent)

    sent = remaining = archived = 0
    for queue in queues:
        try:
            s, r, a = outbox.flush(
                queue, _send if queue == me else _send_as_me,
                is_transport_error=lambda e: isinstance(e, httpx.TransportError))
        except Exception as exc:  # noqa: BLE001 - logged; the read must go on
            _hook_log(f"{hook_event or 'inbox'} outbox flush failed: "
                      f"{type(exc).__name__}: {exc}")
            return
        sent, remaining, archived = sent + s, remaining + r, archived + a
    note = f"outbox flush as {me}: sent {sent}, remaining {remaining}, archived {archived}"
    if hook_event:
        _hook_log(f"{hook_event} {note}")
    elif sent or archived:
        print(f"awrelay: {note}", file=sys.stderr)


def _hook_log(msg: str) -> None:
    try:
        from datetime import datetime, timezone
        path = os.path.join(os.path.expanduser("~"), ".aither", "logs", "relay-inbox.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except OSError:
        return  # the log is where a hook failure goes; its own failure has nowhere to go


def _cmd_inbox(args: argparse.Namespace) -> int:
    """What peers said that THIS session has not seen. `--claude-hook` makes the same verb
    a Claude Code hook (SessionStart / UserPromptSubmit): hook JSON on stdin, additional
    context on stdout, and NEVER a non-zero exit -- a relay outage must not block a prompt."""
    from awrelay import inbox

    hook_event = ""
    if args.claude_hook:
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            data = {}
        # An explicit AWRELAY_SESSION_ID names the session for sends too, so it must win
        # here or a session would read as one nick and write as another.
        args.session_id = (os.environ.get("AWRELAY_SESSION_ID", "").strip()
                           or str(data.get("session_id") or ""))
        hook_event = str(data.get("hook_event_name") or "UserPromptSubmit")
    try:
        client = _client_from_args(args)
        if args.claude_hook:
            client._client.timeout = 6.0  # noqa: SLF001 - a prompt waits on this
        me = client.nick or ""
        if not me:
            raise RelayError("the relay did not say who this bearer is")
        direct_only = bool(getattr(args, "direct_only", False))
        min_interval = float(getattr(args, "min_interval", 0.0) or 0.0)
        if direct_only and min_interval > 0:
            # Throttle BEFORE the network: PostToolUse fires every few seconds.
            since = time.time() - inbox.last_inturn_at(me)
            if since < min_interval:
                return 0
            inbox.mark_inturn(me, time.time())
        # Flush what could not be sent while the relay was down BEFORE reading:
        # a peer's answer to a line still in the queue cannot exist yet. Sits
        # after the in-turn throttle on purpose -- one network touch per window.
        _flush_outbox(client, me, hook_event)
        cursor = "" if args.all else inbox.read_cursor(me, args.channel)
        direct_cursor = inbox.read_cursor(me, inbox.direct_key(args.channel))
        rows = list(client.history(args.channel, limit=args.limit))
        if direct_only:
            # The in-turn read: its cursor is the newest ADDRESSED row delivered and
            # lives beside the channel's, so the prompt-time read is untouched.
            delivered, newest = inbox.select(rows, me=me, identity=client.identity_nick,
                                             cursor=direct_cursor or cursor,
                                             direct_only=True)
            cursor_key = inbox.direct_key(args.channel)
        else:
            delivered, newest = inbox.select(rows, me=me, identity=client.identity_nick,
                                             cursor=cursor, direct_cursor=direct_cursor)
            cursor_key = args.channel
        if not args.peek and not inbox.write_cursor(me, cursor_key, newest):
            note = f"could not save the read cursor for {me}: these messages will repeat"
            if args.claude_hook:
                _hook_log(note)
            else:
                print(f"awrelay: {note}", file=sys.stderr)
    except (RelayError, SystemExit, OSError, ValueError) as exc:
        if args.claude_hook:
            _hook_log(f"{hook_event} inbox {args.channel}: {type(exc).__name__}: {exc}")
            return 0
        print(f"awrelay: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - httpx transport errors; same contract
        if args.claude_hook:
            _hook_log(f"{hook_event} inbox {args.channel}: {type(exc).__name__}: {exc}")
            return 0
        print(f"awrelay: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    text = inbox.frame(delivered, me=me, channel=args.channel)
    if args.claude_hook:
        if hook_event == "SessionStart":
            # A session is told its relay name even when the inbox is empty: a session that
            # does not know it HAS a name never signs with it or looks for replies to it.
            intro = (f"Relay identity: you are `{me}` on {args.channel}. Leave findings and "
                     f"blockers for other sessions with `awrelay send '{args.channel}' "
                     "\"<text>\" --kind finding [--to <nick>]`; replies addressed to you arrive "
                     "here automatically at your next prompt.")
            text = intro + ("\n\n" + text if text else "")
        _hook_log(f"{hook_event} inbox {args.channel} as {me}: delivered {len(delivered)}")
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": hook_event,
                                                     "additionalContext": text}}))
        return 0
    if args.json:
        print(json.dumps({"me": me, "channel": args.channel, "messages": delivered}))
    else:
        print(text or f"(nothing new for {me} in {args.channel})")
    return 0


def _cmd_install_hooks(args: argparse.Namespace) -> int:
    from awrelay.install import install
    return install(user=not args.project, dry_run=args.dry_run, uninstall=args.uninstall)


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
    p_send.add_argument("--to", action="append", default=[],
                         help="address one session: its relay nick, or its session-id prefix "
                              "(repeatable). It arrives in THAT session's inbox marked TO YOU")
    p_send.add_argument("--session-id", default="",
                         help="sign as this session (default: CLAUDE_CODE_SESSION_ID / "
                              "AWRELAY_SESSION_ID / AGENT_SESSION_ID)")
    p_send.set_defaults(func=_cmd_send)

    p_inbox = sub.add_parser(
        "inbox", help="what other sessions said that this one has not seen")
    p_inbox.add_argument("--channel", default=os.environ.get("AWRELAY_INBOX_CHANNEL", "#agents"))
    p_inbox.add_argument("--limit", type=int, default=80, help="rows to scan (default 80)")
    p_inbox.add_argument("--peek", action="store_true", help="do not advance the read cursor")
    p_inbox.add_argument("--all", action="store_true", help="ignore the cursor (last hour)")
    p_inbox.add_argument("--session-id", default="")
    p_inbox.add_argument("--direct-only", action="store_true",
                         help="in-turn read (PostToolUse): only rows addressed to me; "
                              "broadcasts wait for the prompt; nothing is shown twice")
    p_inbox.add_argument("--min-interval", type=float, default=0.0,
                         help="with --direct-only: seconds between relay reads (a working "
                              "turn calls tools every few seconds; do not ask each time)")
    p_inbox.add_argument("--claude-hook", action="store_true",
                          help="run as a Claude Code hook: JSON on stdin, context on stdout, "
                               "always exit 0")
    p_inbox.set_defaults(func=_cmd_inbox)

    p_hooks = sub.add_parser(
        "install-hooks",
        help="register the inbox as a Claude Code SessionStart/UserPromptSubmit hook")
    p_hooks.add_argument("--project", action="store_true",
                          help="write ./.claude/settings.json instead of ~/.claude/settings.json")
    p_hooks.add_argument("--dry-run", action="store_true")
    p_hooks.add_argument("--uninstall", action="store_true")
    p_hooks.set_defaults(func=_cmd_install_hooks)

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


def main(argv: list[str] | None = None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    # GENERATED repo-state intercept (gen_aw_doctor.py) -- do not edit
    try:
        from awgit import state as _aw_state
    except Exception:
        _aw_state = None
    if _aw_state is not None:
        _sv = locals().get("argv")
        if _aw_state.cli_banner(_sv if _sv is not None else __import__("sys").argv[1:]):
            return 0
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.command == "mcp":
        from awrelay.mcp_server import main as mcp_main
        return mcp_main()
    try:
        return args.func(args)
    except httpx.HTTPError as exc:
        # Transport failures are the relay saying nothing at all. They get the same one-line
        # verdict and exit 1 as a refusal -- a 60-line traceback reads as "awrelay is broken"
        # when the truth is "the relay did not answer".
        print(f"awrelay: the relay did not answer ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
