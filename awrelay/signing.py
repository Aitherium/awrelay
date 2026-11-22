"""signing — an Ed25519 signature on an awrelay Envelope.

WHY A MESSAGE NEEDS A SIGNATURE AT ALL
--------------------------------------
AitherRelay's wire format is a channel plus human-readable `content`; nothing in
it proves who wrote a line or that the line is the one they wrote. A `request`
envelope that says "run this job" and an `ack` that says "the council approves"
are therefore claims, not evidence. The collective governance design in
``AitherOS/docs/architecture/relay-swarm-nervous-system.md`` rests on the signed
record: a tally that cannot distinguish a member's vote from a forgery is a poll,
not a decision.

WHY ASYMMETRIC (Ed25519), NOT HMAC
----------------------------------
Same measurement that shaped ``awseal/keys.py``: HMAC verification requires the
signing secret, so every verifier is also a forger, and the code awseal replaced
defaulted to a literal key in source. Ed25519 splits the halves — a private key
signs, a public key verifies — so the relay, a member node and a human can all
check a message without being able to mint one.

WHAT IS SIGNED
--------------
The canonical JSON of ``{kind, sender, text, payload, correlation_id, sent_at}``
— exactly the body ``Envelope.to_relay_content`` serialises. The signature is
carried beside that body inside the same ``awrelay`` fence, so the bytes a
verifier checks are the bytes the relay stored. ``sig`` itself is never part of
what is signed (a signature cannot cover itself).

WHAT A VALID SIGNATURE DOES AND DOES NOT PROVE
----------------------------------------------
It proves *integrity* (the content was not altered after signing) and *key
non-repudiation* (this key signed this envelope). It does NOT prove the key
belongs to the nick: that is a trust registry, and until a nick is pinned the
result reports ``key_trusted: null`` — "internally consistent", never "verified
author". Three separate answers, like ``awseal.verify``, because a single
boolean cannot express "valid signature, wrong key".

FAIL CLOSED
-----------
No default key, no fallback, no "unsigned but returns True". A missing
``cryptography`` package or key file is an error at the moment of use.
"""

from __future__ import annotations

import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from awrelay.envelope import Envelope

try:  # pragma: no cover - exercised by the import-failure path in tests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAVE_CRYPTO = False

__all__ = [
    "SigningError",
    "ALGORITHM",
    "DEFAULT_KEY_PATH",
    "KEY_PATH_ENV",
    "canonical_payload",
    "generate_key_pair",
    "load_private_key",
    "load_public_key",
    "public_key_hex",
    "sign_envelope",
    "verify_envelope",
]

ALGORITHM = "ed25519"

#: Where a private key lives when not given explicitly. A file, not an env var:
#: an env var holding a private key leaks into `ps`, into child processes, and
#: into every subprocess log that dumps its environment.
DEFAULT_KEY_PATH = Path.home() / ".aither" / "awrelay" / "signing.key"

#: Env var naming an ALTERNATIVE key FILE. Deliberately a path, never the key
#: material itself, for the reason above.
KEY_PATH_ENV = "AWRELAY_KEY_PATH"


class SigningError(RuntimeError):
    """Signing or verification could not be performed. Fail closed."""


def _require_crypto() -> None:
    if not _HAVE_CRYPTO:
        raise SigningError(
            "the `cryptography` package is required to sign or verify an awrelay "
            "envelope. Install `awrelay[sign]`. A signing library that degrades to "
            "'unsigned, continuing' is the failure this module exists to prevent"
        )


# --- keys -------------------------------------------------------------------


def _default_path() -> Path:
    env = os.environ.get(KEY_PATH_ENV)
    return Path(env) if env else DEFAULT_KEY_PATH


def generate_key_pair(path: Optional[str | os.PathLike[str]] = None,
                      *, overwrite: bool = False) -> Path:
    """Create a new Ed25519 private key. Returns the path written.

    Refuses to overwrite by default: a regenerated key silently invalidates every
    signature ever made with the old one, and the symptom — "old messages stopped
    verifying" — does not point at the command that caused it.
    """
    _require_crypto()
    target = Path(path or _default_path())
    if target.exists() and not overwrite:
        raise SigningError(
            f"{target} already exists. Overwriting it invalidates every signature "
            f"made with the current key, and nothing downstream can tell that from "
            f"tampering. Pass overwrite=True only if you mean it")
    target.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    target.write_bytes(pem)
    try:
        os.chmod(target, 0o600)
    except OSError as exc:
        # Windows does not honour POSIX modes; the key may be readable by other
        # accounts. A comment is not a warning, so emit one.
        warnings.warn(
            f"could not restrict permissions on {target} ({exc}). The private key "
            f"may be readable by other users on this machine.",
            RuntimeWarning, stacklevel=2)
    return target


def load_private_key(path: Optional[str | os.PathLike[str]] = None):
    """Load the signing key. Raises if absent — there is deliberately no default."""
    _require_crypto()
    target = Path(path or _default_path())
    if not target.is_file():
        raise SigningError(
            f"no awrelay signing key at {target}. Create one with "
            f"`awrelay signing-keygen`. There is no default key: signing with a "
            f"constant makes every signature forgeable by anyone who can read the "
            f"source")
    try:
        return serialization.load_pem_private_key(target.read_bytes(), password=None)
    except Exception as exc:  # noqa: BLE001 - any failure here is unusable
        raise SigningError(f"cannot load private key {target}: {exc}") from exc


def public_key_hex(private_key=None,
                   path: Optional[str | os.PathLike[str]] = None) -> str:
    """The public half, hex-encoded — safe to publish, and meant to be."""
    _require_crypto()
    key = private_key or load_private_key(path)
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def load_public_key(hex_key: str):
    """A verifier's view: a public key from its raw Ed25519 hex form."""
    _require_crypto()
    if not isinstance(hex_key, str) or not hex_key.strip():
        raise SigningError("public key must be a non-empty hex string")
    try:
        raw = bytes.fromhex(hex_key.strip())
    except ValueError as exc:
        raise SigningError(f"public key is not valid hex: {exc}") from exc
    if len(raw) != 32:
        raise SigningError(
            f"an Ed25519 public key is 32 bytes; got {len(raw)}. Refusing rather "
            f"than attempting a verification that cannot succeed")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise SigningError(f"not a valid Ed25519 public key: {exc}") from exc


# --- the signed payload -----------------------------------------------------


def canonical_payload(envelope: Envelope) -> bytes:
    """The one serialisation both signer and verifier must agree on.

    It covers every field that carries meaning — the wire body AND the visible
    `text` (see ``Envelope.signed_dict``), minus the signature that describes
    them. ``sort_keys`` and compact separators make the bytes independent of
    dict insertion order and of Python's whitespace choices.
    """
    return json.dumps(
        envelope.signed_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_envelope(envelope: Envelope, *,
                  key_path: Optional[str | os.PathLike[str]] = None,
                  now: Optional[str] = None) -> Envelope:
    """Sign ``envelope`` in place and return it.

    ``key_path`` overrides :data:`DEFAULT_KEY_PATH` / ``AWRELAY_KEY_PATH``.
    ``now`` is an ISO-8601 string for reproducible tests; it describes the
    signing moment and is not part of the signed payload.
    """
    private = load_private_key(key_path)
    signature = private.sign(canonical_payload(envelope)).hex()
    envelope.sig = {
        "alg": ALGORITHM,
        "public_key": public_key_hex(private),
        "signature": signature,
        "signed_at": now or datetime.now(timezone.utc).isoformat(),
    }
    return envelope


def verify_envelope(envelope: Envelope, *,
                    expect_key: Optional[str] = None) -> dict[str, Any]:
    """Verify a signed envelope. Reports three answers, never one boolean.

    Returns ``{algorithm, public_key, signature_ok, key_trusted, ok, reason}``.

    * ``signature_ok`` — did this key sign this exact payload
    * ``key_trusted``  — is the key the one the caller pinned (``None`` when the
      caller pinned nothing, which is not the same as "trusted")
    * ``ok``           — signature verified AND the key was not known-wrong
    """
    result: dict[str, Any] = {
        "algorithm": "",
        "public_key": "",
        "signature_ok": False,
        "key_trusted": None,
        "ok": False,
        "reason": "",
    }
    sig = envelope.sig
    if not isinstance(sig, dict) or not sig:
        result["reason"] = "envelope is unsigned"
        return result

    algorithm = str(sig.get("alg") or "").strip().lower()
    result["algorithm"] = algorithm
    if algorithm != ALGORITHM:
        result["reason"] = (
            f"unsupported signature algorithm {algorithm!r}; only {ALGORITHM!r} "
            f"is accepted")
        return result

    public_key = str(sig.get("public_key") or "").strip().lower()
    signature_hex = str(sig.get("signature") or "").strip()
    result["public_key"] = public_key
    if not public_key or not signature_hex:
        result["reason"] = "signature block is missing public_key or signature"
        return result

    try:
        pub = load_public_key(public_key)
        pub.verify(bytes.fromhex(signature_hex), canonical_payload(envelope))
        result["signature_ok"] = True
    except Exception as exc:  # noqa: BLE001 - any failure is a failed verification
        result["reason"] = f"signature does not verify: {exc}"
        return result

    if expect_key is not None:
        result["key_trusted"] = (public_key == str(expect_key).strip().lower())

    result["ok"] = bool(result["signature_ok"] and result["key_trusted"] is not False)
    if not result["ok"]:
        result["reason"] = "signature is valid but the key is not the one pinned for this sender"
    return result
