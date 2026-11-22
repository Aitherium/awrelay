"""An awrelay Envelope can be signed, and the signature survives the wire.

The signature is the evidence the collective governance design rests on: a vote
or an approval that cannot be verified is a claim. These tests pin the parts a
verifier depends on — the exact signed bytes, the round-trip through the fence,
and the refusal of anything altered after signing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure this tree's package is importable when the suite is run from the
# repo root. Do NOT evict `awrelay` from sys.modules: collection imports every
# test module first, and dropping modules another test already holds breaks it
# with a class-identity mismatch that looks unrelated to signing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awrelay import signing  # noqa: E402
from awrelay.client import RelayClient  # noqa: E402
from awrelay.envelope import Envelope  # noqa: E402


def _env(text: str = "found a race in the retry loop") -> Envelope:
    return Envelope.new("finding", "dana+a3b4c1d2", text, payload={"file": "q.py", "line": 12})


def _key(tmp_path: Path) -> Path:
    return signing.generate_key_pair(tmp_path / "signing.key")


def test_sign_then_verify(tmp_path):
    key = _key(tmp_path)
    env = signing.sign_envelope(_env(), key_path=key, now="2026-11-22T00:00:00+00:00")
    assert env.sig and env.sig["alg"] == "ed25519"
    assert len(env.sig["public_key"]) == 64
    result = signing.verify_envelope(env)
    assert result["signature_ok"] is True
    assert result["ok"] is True
    assert result["key_trusted"] is None, "no pin supplied is 'unknown', not 'untrusted'"


def test_signature_round_trips_through_the_relay_fence(tmp_path):
    env = signing.sign_envelope(_env(), key_path=_key(tmp_path))
    rendered = env.to_relay_content()
    parsed = Envelope.from_relay_content(rendered)
    assert parsed is not None
    assert parsed.sig == env.sig
    assert signing.verify_envelope(parsed)["ok"] is True


def test_tampering_after_signing_breaks_verification(tmp_path):
    env = signing.sign_envelope(_env(), key_path=_key(tmp_path))
    env.text = "something the author never wrote"
    result = signing.verify_envelope(env)
    assert result["signature_ok"] is False
    assert result["ok"] is False


def test_payload_dict_order_does_not_change_the_signed_bytes(tmp_path):
    key = _key(tmp_path)
    a = Envelope.new("finding", "dana", "x", payload={"a": 1, "b": 2})
    b = Envelope.new("finding", "dana", "x", payload={"b": 2, "a": 1})
    # Same fields, same timestamps: force them equal so only key order differs.
    b.sent_at = a.sent_at
    signing.sign_envelope(a, key_path=key, now="t")
    signing.sign_envelope(b, key_path=key, now="t")
    assert a.sig["signature"] == b.sig["signature"]


def test_a_pinned_key_that_is_not_the_signers_is_not_ok(tmp_path):
    env = signing.sign_envelope(_env(), key_path=_key(tmp_path))
    result = signing.verify_envelope(env, expect_key="ab" * 32)
    assert result["signature_ok"] is True
    assert result["key_trusted"] is False
    assert result["ok"] is False


def test_a_pinned_key_that_matches_is_ok(tmp_path):
    key = _key(tmp_path)
    env = signing.sign_envelope(_env(), key_path=key)
    result = signing.verify_envelope(env, expect_key=signing.public_key_hex(path=key))
    assert result["key_trusted"] is True
    assert result["ok"] is True


def test_unsigned_is_not_verified():
    result = signing.verify_envelope(_env())
    assert result["signature_ok"] is False
    assert result["ok"] is False
    assert "unsigned" in result["reason"]


def test_an_unknown_algorithm_is_refused(tmp_path):
    env = signing.sign_envelope(_env(), key_path=_key(tmp_path))
    env.sig["alg"] = "hmac-sha256"
    result = signing.verify_envelope(env)
    assert result["ok"] is False
    assert "algorithm" in result["reason"]


def test_the_signature_block_is_never_signed_but_the_text_is():
    env = _env()
    assert "sig" not in env.body_dict()
    signed = env.signed_dict()
    assert "sig" not in signed
    assert signed["text"] == env.text, (
        "the visible message must be covered; a signature over only the fence "
        "body would let the text be rewritten after signing")


def test_signing_without_a_key_fails_closed(tmp_path):
    missing = tmp_path / "nope.key"
    try:
        signing.sign_envelope(_env(), key_path=missing)
    except signing.SigningError as exc:
        assert "no awrelay signing key" in str(exc)
    else:  # pragma: no cover - a default key appearing is the bug this guards
        raise AssertionError("signing with no key must raise, never fall back")


def test_client_send_signs_when_asked(tmp_path):
    key = _key(tmp_path)
    client = RelayClient("https://relay.example", nick="dana+a3b4c1d2")
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True}

    def fake_write(method, path, channel, **kw):
        captured.update(kw["json"])
        return _Resp()

    client._write = fake_write  # type: ignore[assignment]
    client.send("#agents", _env(), sign=True, key_path=str(key))

    parsed = Envelope.from_relay_content(captured["content"])
    assert parsed is not None and parsed.sig
    assert signing.verify_envelope(parsed)["ok"] is True


def test_client_send_is_unsigned_by_default(tmp_path):
    client = RelayClient("https://relay.example", nick="dana+a3b4c1d2")
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"success": True}

    def fake_write(method, path, channel, **kw):
        captured.update(kw["json"])
        return _Resp()

    client._write = fake_write  # type: ignore[assignment]
    client.send("#agents", _env())

    parsed = Envelope.from_relay_content(captured["content"])
    assert parsed is not None and parsed.sig is None, (
        "signing must stay opt-in so existing clients and the Discord mirror are "
        "byte-for-byte unchanged")
