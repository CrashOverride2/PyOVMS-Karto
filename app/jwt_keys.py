"""
Public key material for verifying the OVMS main server's session tokens.

Karto used to hold the main server's *symmetric* SECRET_KEY_JWT so it could validate
session cookies. With HS256 the verifying key is also the signing key, so anything able
to read Karto's configuration could mint a token for any user of the main server —
administrators included. Karto never needed that capability; it only verifies.

The main server now signs with an Ed25519 private key it alone holds, and Karto is given
just the matching public key. A compromise here can no longer produce a valid session
anywhere. Deliberately, there is no way to sign from this module.

JWT_PUBLIC_KEY is base64 of the raw 32-byte Ed25519 public key — the value the main
server prints on key generation (`python -m app.jwt_keys` there).
"""

import base64
import functools

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import settings

JWT_ALGORITHM = "EdDSA"

# Must match the main server. Session tokens carry this audience so another token type
# signed with the same key cannot be replayed here as a session.
JWT_AUDIENCE = "ovms-session"
JWT_ISSUER = "ovms-server"


class JwtKeyError(RuntimeError):
    """Raised when the configured public key is missing or malformed."""


@functools.lru_cache(maxsize=1)
def get_public_key() -> Ed25519PublicKey:
    if not settings.JWT_PUBLIC_KEY:
        raise JwtKeyError(
            "JWT_PUBLIC_KEY is not set. Copy it from the OVMS main server's .env "
            "(only the public half — Karto must never hold the private key)."
        )
    try:
        raw = base64.b64decode(settings.JWT_PUBLIC_KEY, validate=True)
    except Exception as exc:
        raise JwtKeyError(f"JWT_PUBLIC_KEY is not valid base64: {exc}") from exc
    if len(raw) != 32:
        raise JwtKeyError(
            f"JWT_PUBLIC_KEY must decode to exactly 32 bytes, got {len(raw)}. "
            "Make sure you copied the public key, not the private one or a PEM block."
        )
    return Ed25519PublicKey.from_public_bytes(raw)


def _reset_cache() -> None:
    """Drop the memoised key (tests change the settings object at runtime)."""
    get_public_key.cache_clear()
