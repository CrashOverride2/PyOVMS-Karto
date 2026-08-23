"""
Shared setup for the Karto security tests.

These are offline and import-level: no broker, no PostGIS, no network. app/config.py
reads the environment at import time and has required fields with no defaults, so the
values below must be in place before anything from `app` is imported.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Public half of a fixed Ed25519 key pair, matching the main server's test seed so a
# token signed there verifies here. Karto only ever gets the public key.
import base64  # noqa: E402

TEST_JWT_SEED = b"\x11" * 32


def _test_public_key_b64() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(TEST_JWT_SEED)
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode()


os.environ.setdefault("JWT_PUBLIC_KEY", _test_public_key_b64())
os.environ.setdefault("MQTT_BROKER_HOST", "localhost")
os.environ.setdefault("MQTT_USER", "karto_test")
os.environ.setdefault("MQTT_PASSWORD", "karto_test_password")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OVMS_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("FORWARDED_ALLOW_IPS", "127.0.0.1")
