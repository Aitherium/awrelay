"""A2ABridge — a scoped, external-shaped client for AitherA2A.

WHY THIS IS SAFE TO BUILD NOW, WHEN `envelope.py` SAYS IT ISN'T YET
--------------------------------------------------------------------
`envelope.py`'s docstring defers "a real A2A bridge" because AitherA2A's own
authorization (`_authorize_a2a` in `services/agents/AitherA2A.py`) has a
documented gap: the FIRST branch is `if verify_internal_key(...): return
"internal"` — any in-fleet caller holding `AITHER_INTERNAL_SECRET` skips the
per-skill grant system entirely and reaches every skill on every registered
agent. That is this repo's ordinary fleet-trust model (dozens of other
services rely on it) and is not being changed here.

This module is safe because it structurally cannot hit that branch: it never
reads or sends `AITHER_INTERNAL_SECRET` / `X-Internal-Key` anywhere below —
grep this file for either string and find nothing. Every call it makes is
authenticated the way a genuine external peer's is, through the SAME
approval-card + grant-token flow `_authorize_a2a` already enforces for
everyone who isn't an in-fleet caller: a 403 carrying an `access_request_id`
until a human approves it via `POST /access-requests/{id}/approve` (owner-only,
requires the internal key — this module cannot and does not call that
endpoint itself) and hands the resulting token back.

NOT PROTOCOL-COMPLIANT A2A
---------------------------
`kind` maps loosely onto A2A's message/task/artifact split, per
`envelope.py`'s own note — this does not claim Google A2A protocol
compliance. A `REQUEST` envelope naming `service`/`skill` in its payload
becomes an AitherA2A `/call`; one naming `to` (and no `service`/`skill`)
becomes a `/delegate`. Nothing else is translated.

THE APPROVAL LOOP IS NOT AUTOMATED, ON PURPOSE
------------------------------------------------
`/access-requests/{id}/approve` requires the internal key, so this bridge
cannot complete its own approval even if it wanted to — the human-in-the-loop
step is enforced by AitherA2A itself, not by discipline here. A denied call
raises `A2AApprovalPendingError` naming the `access_request_id`; whoever is driving
the bridge is responsible for getting that in front of a human (posting it
into the awrelay channel the request came from is the obvious move — see
`bridge_and_reply()`) and, once approved, calling `set_grant()` with the
returned token before retrying.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

A2A_TOKEN_HEADER = "X-A2A-Grant"


class A2AError(Exception):
    """The A2A gateway refused or could not be reached for a reason other
    than a pending approval (see `A2AApprovalPendingError`)."""


class A2AApprovalPendingError(Exception):
    """The call was denied and a permission card was raised. Carries the
    `access_request_id` a human approves via AitherA2A's own admin surface
    (`POST /access-requests/{id}/approve`, owner-only) — this bridge cannot
    approve its own request, by design.
    """

    def __init__(self, resource: str, access_request_id: str, how_to_proceed: str) -> None:
        self.resource = resource
        self.access_request_id = access_request_id
        self.how_to_proceed = how_to_proceed
        super().__init__(
            f"approval needed for {resource!r} (access_request_id={access_request_id!r})"
        )


class A2ABridge:
    def __init__(
        self, base_url: str, *, timeout: float = 60.0, verify: bool | str = True
    ) -> None:
        """
        base_url  AitherA2A's origin, e.g. "https://a2a.aitherium.com" or a
                  self-hosted instance's `http://host:8766`.
        verify    passed straight to httpx — never `False`
                  (security-review-patterns.md #4); a self-hosted gateway
                  with a private CA should pass its CA bundle path here.

        Deliberately takes NO internal-key parameter of any kind. If a
        caller needs one to reach AitherA2A, it should call AitherA2A
        directly — that is a different trust boundary than this bridge
        exists to preserve.
        """
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, verify=verify)
        self._grants: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "A2ABridge":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def set_grant(self, resource: str, token: str) -> None:
        """Register a grant token for `resource` (e.g. "a2a.skill.AitherWorkingMemory.save"
        for a `/call`, "a2a.delegate.artist" for a `/delegate`) after a human
        has approved the matching `access_request_id` out of band. Subsequent
        calls needing that exact resource send this token."""
        self._grants[resource] = token

    def _headers(self, resource: str) -> dict[str, str]:
        token = self._grants.get(resource)
        return {A2A_TOKEN_HEADER: token} if token else {}

    def _post(self, path: str, resource: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=body, headers=self._headers(resource))
        if resp.status_code == 403:
            detail = {}
            try:
                detail = resp.json().get("detail", {})
            except Exception:  # noqa: BLE001 — a non-JSON 403 falls through to A2AError below
                detail = {}
            if isinstance(detail, dict) and detail.get("access_request_id"):
                raise A2AApprovalPendingError(
                    resource=detail.get("resource", resource),
                    access_request_id=detail["access_request_id"],
                    how_to_proceed=detail.get("how_to_proceed", ""),
                )
            raise A2AError(f"POST {path} -> 403: {resp.text[:200]}")
        if resp.status_code != 200:
            raise A2AError(f"POST {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def call(
        self, service: str, skill: str, params: Optional[dict[str, Any]] = None,
        *, grant_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """AitherA2A `/call` — invoke `skill` on a registered service.
        Raises `A2AApprovalPendingError` on first use until a human grants
        "a2a.skill.{service}.{skill}:execute" (the exact resource string
        `_authorize_a2a` checks — matched here so `set_grant` targets the
        right key). Pass `grant_token` to register one just-in-time instead
        of calling `set_grant` separately — useful for a stateless caller
        (e.g. an MCP tool) that received a token out of band and has nowhere
        durable to hold a bridge instance between calls."""
        resource = f"a2a.skill.{service}.{skill}:execute"
        if grant_token:
            self.set_grant(resource, grant_token)
        return self._post(
            "/call", resource,
            {"service": service, "skill": skill, "params": params or {}},
        )

    def delegate(
        self, to_agent: str, message: str, *,
        from_agent: str = "awrelay-bridge", session_id: Optional[str] = None,
        grant_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """AitherA2A `/delegate` — hand a task to another agent. Raises
        `A2AApprovalPendingError` until a human grants
        "a2a.delegate.{to_agent}:execute". See `call()` for `grant_token`."""
        resource = f"a2a.delegate.{to_agent}:execute"
        if grant_token:
            self.set_grant(resource, grant_token)
        body: dict[str, Any] = {"from": from_agent, "to": to_agent, "message": message}
        if session_id:
            body["sessionId"] = session_id
        return self._post("/delegate", resource, body)

    def call_envelope(self, env: "Any") -> dict[str, Any]:  # Envelope, avoid a hard import cycle
        """Translate a REQUEST-kind Envelope into `/call` or `/delegate`,
        per this module's mapping. Raises ValueError for any other kind —
        callers should route MESSAGE/FINDING/ALERT/STEER/ACK through the
        ordinary awrelay channel, not through here.
        """
        from awrelay.envelope import Kind

        if env.kind != Kind.REQUEST:
            raise ValueError(
                f"only REQUEST envelopes map onto A2A; got kind={env.kind!r}. "
                "Post other kinds through the channel with RelayClient instead."
            )
        payload = env.payload or {}
        service = payload.get("service")
        skill = payload.get("skill")
        if service and skill:
            return self.call(service, skill, payload.get("params"))
        to_agent = payload.get("to")
        if to_agent:
            return self.delegate(
                to_agent, env.text, from_agent=env.sender, session_id=payload.get("sessionId"),
            )
        raise ValueError(
            "REQUEST envelope payload must carry either {service, skill} for a "
            "/call or {to} for a /delegate"
        )


def bridge_and_reply(
    bridge: A2ABridge, relay: "Any", channel: str, env: "Any"
) -> dict[str, Any] | A2AApprovalPendingError:
    """Attempt `bridge.call_envelope(env)`; on `A2AApprovalPendingError`, post
    the `access_request_id` back into the awrelay channel the REQUEST came
    from — rather than raising into a caller that may have nothing watching
    for it — so a human reading the channel sees there is something to
    approve instead of the request silently never completing.

    `relay` is a `RelayClient`, taken by parameter rather than imported at
    module load to avoid a hard import cycle with `client.py`.

    Returns the `/call` or `/delegate` result dict on success, or the
    `A2AApprovalPendingError` instance (not raised) when a card was raised —
    inspect `.access_request_id` if the caller needs it beyond what was
    already posted to the channel. A genuine `A2AError` (gateway down, a
    malformed response) still raises; that is not "wait for a human", it is
    a real failure the caller should see.
    """
    try:
        return bridge.call_envelope(env)
    except A2AApprovalPendingError as exc:
        relay.send_text(
            channel,
            f"A2A approval needed for {exc.resource}: {exc.how_to_proceed}",
            kind="alert",
            payload={
                "access_request_id": exc.access_request_id,
                "resource": exc.resource,
            },
            correlation_id=env.correlation_id,
        )
        return exc
