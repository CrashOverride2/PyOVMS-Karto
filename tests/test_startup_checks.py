"""
Regression tests for C-3 and the move to asymmetric session tokens.

Karto validates the OVMS main server's session cookies itself. It used to do so with the
main server's *symmetric* SECRET_KEY_JWT, and the shipped .env-template contained a
working placeholder that nothing checked — so a deployment that copied the template
as-is accepted tokens anyone could mint:

    {"sub": "<any username>", "mfa": true, "ver": 0, "exp": <future>}

With an admin's username that also unlocks the admin bypass in
crud.check_vehicle_ownership: every vehicle's trip history, including deletion. It goes
unnoticed in practice because API-key auth keeps working; only the cookie path breaks,
and that is rarely exercised during setup.

Karto now holds only the Ed25519 *public* key, so even a full compromise of this host
cannot produce a valid session anywhere.
"""

import base64

import pytest

from app import jwt_keys, startup_checks


@pytest.fixture
def settings():
    from app.config import settings as live_settings
    return live_settings


@pytest.fixture(autouse=True)
def _clear_key_cache():
    jwt_keys._reset_cache()
    yield
    jwt_keys._reset_cache()


def test_missing_public_key_aborts_startup(monkeypatch, settings):
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", None)
    with pytest.raises(RuntimeError, match="JWT_PUBLIC_KEY"):
        startup_checks.check_jwt_configuration()


@pytest.mark.parametrize("bad", [
    "not-base64!!",
    base64.b64encode(b"too-short").decode(),
    base64.b64encode(b"x" * 64).decode(),   # e.g. a PEM body or the wrong key type
])
def test_malformed_public_key_aborts_startup(monkeypatch, settings, bad):
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", bad)
    with pytest.raises(RuntimeError):
        startup_checks.check_jwt_configuration()


def test_a_valid_public_key_passes(settings):
    startup_checks.check_jwt_configuration()  # conftest supplies a real key


def test_karto_cannot_sign_tokens(settings):
    """
    The property the migration rests on: this host holds no signing capability.
    """
    import jwt as pyjwt

    with pytest.raises(Exception):
        pyjwt.encode({"sub": "admin"}, jwt_keys.get_public_key(), algorithm="EdDSA")


def test_leftover_symmetric_secret_is_flagged(monkeypatch, settings, caplog):
    """A stale SECRET_KEY_JWT on this host is exactly what the change removes."""
    monkeypatch.setattr(settings, "SECRET_KEY_JWT", "a-real-looking-leftover-secret-value")
    with caplog.at_level("WARNING"):
        startup_checks.check_jwt_configuration()
    assert any("SECRET_KEY_JWT" in record.message for record in caplog.records)


def test_algorithm_is_fixed_to_eddsa():
    assert jwt_keys.JWT_ALGORITHM == "EdDSA"
    assert not hasattr(jwt_keys, "sign"), "this module must never gain a signing helper"


def test_token_issued_by_the_main_server_verifies_here():
    """
    Interop guard. The claim set and algorithm are agreed across two repositories, so a
    change on either side (audience string, issuer, algorithm) must fail loudly here
    rather than silently reject every user's session in production.
    """
    import datetime

    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tests.conftest import TEST_JWT_SEED

    # Exactly what app/security.py on the main server builds.
    token = pyjwt.encode(
        {
            "sub": "alice",
            "mfa": True,
            "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
            "ver": 0,
            "aud": "ovms-session",
            "iss": "ovms-server",
        },
        Ed25519PrivateKey.from_private_bytes(TEST_JWT_SEED),
        algorithm="EdDSA",
    )

    payload = pyjwt.decode(
        token,
        jwt_keys.get_public_key(),
        algorithms=[jwt_keys.JWT_ALGORITHM],
        audience=jwt_keys.JWT_AUDIENCE,
        issuer=jwt_keys.JWT_ISSUER,
        options={"require": ["sub", "exp", "aud", "iss"]},
    )
    assert payload["sub"] == "alice" and payload["mfa"] is True


def test_token_signed_with_the_old_shared_secret_is_refused():
    """The exact attack the migration removes."""
    import datetime

    import jwt as pyjwt

    forged = pyjwt.encode(
        {
            "sub": "admin",
            "mfa": True,
            "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
            "aud": "ovms-session",
            "iss": "ovms-server",
        },
        "the-old-shared-symmetric-secret",
        algorithm="HS256",
    )
    with pytest.raises(pyjwt.InvalidTokenError):
        pyjwt.decode(
            forged,
            jwt_keys.get_public_key(),
            algorithms=[jwt_keys.JWT_ALGORITHM],
            audience=jwt_keys.JWT_AUDIENCE,
            issuer=jwt_keys.JWT_ISSUER,
        )


# --- proxy trust ------------------------------------------------------------------

@pytest.mark.parametrize("value", ["*", "127.0.0.1,*", "0.0.0.0/0", "::/0", "127.0.0.1, 0.0.0.0/0"])
def test_wildcard_proxy_trust_aborts_startup(monkeypatch, settings, value):
    """
    '*' and a zero-length prefix have the same effect in uvicorn: the leftmost,
    client-controlled X-Forwarded-For entry wins, so a caller picks its own identity for
    the rate limiter — and can drive a victim's address into a ban.
    """
    monkeypatch.setattr(settings, "FORWARDED_ALLOW_IPS", value)
    with pytest.raises(RuntimeError):
        startup_checks.check_proxy_configuration()


@pytest.mark.parametrize("value", ["127.0.0.1", "10.0.0.0/8", "127.0.0.1,10.1.2.3", "proxy.internal"])
def test_concrete_proxy_lists_are_accepted(monkeypatch, settings, value):
    monkeypatch.setattr(settings, "FORWARDED_ALLOW_IPS", value)
    startup_checks.check_proxy_configuration()  # must not raise


# --- the template must not ship a usable secret ------------------------------------

def test_env_template_ships_no_usable_key_and_no_signing_secret():
    """
    Anything in the template is public by definition. It must contain no working key —
    and must no longer ask operators to copy the main server's signing secret here.
    """
    from pathlib import Path

    template = (Path(__file__).resolve().parent.parent / ".env-template").read_text()

    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("JWT_PUBLIC_KEY="):
            assert stripped == "JWT_PUBLIC_KEY=", "the template must not ship a usable key"
            break
    else:
        pytest.fail("JWT_PUBLIC_KEY not found in .env-template")

    active = [
        line.strip() for line in template.splitlines()
        if line.strip().startswith("SECRET_KEY_JWT=")
    ]
    assert not active, (
        "the template must not set SECRET_KEY_JWT any more — Karto verifies with a "
        "public key and must never hold the main server's signing secret"
    )
