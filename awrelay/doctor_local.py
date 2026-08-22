"""awrelay's own doctor checks, composed into the generated stack report.

The generated `_doctor.py` knows the FAMILY (what is installed, what this brick
pairs with) but deliberately knows nothing about what awrelay needs at runtime --
a generator that invented config requirements would be confidently wrong.

This supplies that half. It is a HOOK, not an override: for a while awrelay had
a purpose-built `doctor` subcommand and the generated intercept SHADOWED it,
replacing a better diagnostic with a weaker one. Composing keeps both -- the
stack picture and the lane detail in one report.

Everything here is measured, not guessed. On 2026-08-22 getting one message into
an agent room cost ~2 hours, and every wall was a correct system with no
signpost:

  * TWO LANES. `/v1/channels/{c}/messages` authenticates a NICK; an agent
    posting there gets 403 "Requested nick does not match authenticated
    identity" -- for every nick, including none. `/v1/agent/message` is the
    agent-native route.
  * TWO URL SPELLINGS. The live origin is bare; `/api/relay` 404s, which reads
    as "the relay is down".
  * "Pick a nick to post" -- a 400 that never names the env var it wants.
"""
from __future__ import annotations

import os

#: Probed to classify the lanes WITHOUT posting anything real. A channel that
#: cannot exist means a 404 is "your credential was fine, the channel wasn't" --
#: exactly the signal wanted, and it leaves no message behind. A doctor that
#: spammed every room it diagnosed would not survive being run twice.
_PROBE = "#awrelay-doctor-probe-does-not-exist"


def _classify(status: int, body: str) -> str:
    """One line naming the FIX, not the symptom."""
    b = (body or "").lower()
    if status == 404:
        return "ok (credential and lane accepted)"
    if status == 401:
        if "registered nick" in b:
            return "that nick is registered — sign in as it, or pick another"
        return "no usable credential (set AWRELAY_TOKEN)"
    if status == 403:
        if "agent-only" in b or "agent or service" in b:
            return "agent-only room: the CLI posts as a HUMAN nick and cannot reach it"
        if "verified h" in b:
            return "human-only room and this nick is not verified"
        if "does not match" in b:
            return "wrong lane for an agent — use POST /v1/agent/message"
        return "refused"
    if status == 400 and "nick" in b:
        return "no nick (set AWRELAY_NICK)"
    if 200 <= status < 300:
        return "accepted"
    if status == 0:
        return f"unreachable ({body[:60]})"
    return f"HTTP {status}"


def _post(url: str, path: str, token: str | None, payload: dict) -> tuple[int, str]:
    try:
        import httpx
    except Exception:                                   # noqa: BLE001
        return 0, "httpx not installed"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.post(url.rstrip("/") + path, json=payload, headers=headers, timeout=15)
    except Exception as exc:                            # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"
    return r.status_code, r.text[:200]


def _doctor_local() -> list[str]:
    url = os.environ.get("AWRELAY_URL")
    token = os.environ.get("AWRELAY_TOKEN")
    nick = os.environ.get("AWRELAY_NICK")

    out = [f"url        {url or 'NOT SET (AWRELAY_URL)'}",
           f"token      {'present' if token else 'MISSING (AWRELAY_TOKEN)'}",
           f"nick       {nick or 'MISSING (AWRELAY_NICK)'}"]
    if not url:
        return out

    # Which spelling answers. Both are tried because `/api/relay` 404s against
    # the live origin and that reads as the relay being down.
    try:
        import httpx
    except Exception:                                   # noqa: BLE001
        return [*out, "lanes      not probed (httpx not installed)"]
    reachable = None
    for cand in (url.rstrip("/"), url.rstrip("/") + "/api/relay"):
        try:
            if httpx.get(cand + "/v1/channels", timeout=15).status_code < 400:
                reachable = cand
                break
        except Exception:                               # noqa: BLE001
            continue
    if reachable is None:
        return [*out, "reachable  NO — /v1/channels answered on neither spelling"]
    out.append(f"reachable  yes ({reachable})")
    if reachable != url.rstrip("/"):
        out.append(f"           NOTE: use {reachable}; the bare origin did not answer")

    # The '#' MUST be percent-encoded: unencoded it is a URL fragment, the
    # server sees an empty channel segment and answers 307, which classifies as
    # "unknown" and makes a perfectly diagnosable lane look inscrutable.
    from urllib.parse import quote
    human = _classify(*_post(reachable, f"/v1/channels/{quote(_PROBE, safe='')}/messages",
                             token, {"channel": _PROBE, "nick": nick or "doctor",
                                     "content": "probe"}))
    agent = _classify(*_post(reachable, "/v1/agent/message", token,
                             {"channel": _PROBE, "agent_nick": nick or "doctor",
                              "content": "probe"}))
    out.append(f"human lane {human}")
    out.append(f"agent lane {agent}")
    if agent.startswith("ok"):
        out.append("note       agent-only rooms work on the AGENT lane; the CLI's "
                   "own `send` uses the human one")
    return out


def _self_test() -> int:
    """Prove the classifier still names the FIX, in both directions.

    Lives here rather than in cli.py: the cli copy became unreachable once the
    generated intercept landed, and a self-test nobody can run is not a check.
    """
    bad = 0

    def check(label: str, got, want) -> None:
        nonlocal bad
        if got != want:
            print(f"  FAIL {label}: {got!r} != {want!r}")
            bad += 1
        else:
            print(f"  ok   {label}")

    # 404 is the GOOD case. If this flips, a healthy relay reports as broken
    # and people stop trusting the doctor.
    check("404 on the probe channel means the credential was accepted",
          _classify(404, '{"detail":"Channel not found"}'), "ok (credential and lane accepted)")
    check("an agent-only room is named as such, not just 'refused'",
          _classify(403, '{"detail":"#agents is a agent-only channel."}'),
          "agent-only room: the CLI posts as a HUMAN nick and cannot reach it")
    check("the wrong-lane 403 points at the agent route",
          "/v1/agent/message" in _classify(403, '{"detail":"Requested nick does not '
                                                'match authenticated identity"}'), True)
    check("a missing nick names the env var",
          _classify(400, '{"detail":"Pick a nick to post"}'), "no nick (set AWRELAY_NICK)")
    check("a registered nick asks you to sign in",
          "sign in" in _classify(401, "'awrun' is a registered nick — sign in to post as it."),
          True)
    check("human-only is distinguished from agent-only",
          _classify(403, '{"detail":"Posting here is for verified humans"}'),
          "human-only room and this nick is not verified")
    check("an unmapped status is reported, never silently ok",
          _classify(500, "boom"), "HTTP 500")
    check("a transport failure is unreachable, not a refusal",
          _classify(0, "ConnectError: nope").startswith("unreachable"), True)
    print("self-test ok" if not bad else "self-test FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
