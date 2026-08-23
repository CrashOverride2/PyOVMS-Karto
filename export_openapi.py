#!/usr/bin/env python3
"""
Write this service's OpenAPI schema to a file for the main server to document.

The Karto API lives behind the main server's reverse proxy under /api/karto/v1, so
operators expect to find it in the main server's /docs. That used to be solved by
keeping a copy of this service's routers inside the main repo
(app/services/karto/) purely so FastAPI had route objects to describe. The copy was
executable, it drifted — its /maps authorization still parsed the vehicle id out of
the filename long after this service had replaced that with a database lookup — and
the only thing keeping it harmless was a stub crud module returning False. A
documentation problem does not need executable code in another repository.

This exports the schema instead. The main server merges the JSON into its own
schema; nothing from here is imported or executed over there.

Run it whenever a route, parameter or response model changes:

    .venv/bin/python export_openapi.py

By default it writes into the sibling main-server checkout. Pass a path to override.
The values below are placeholders for required settings — the schema depends only on
the route definitions, never on the configuration — so this runs without a real
.env, a broker or a database.
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("MQTT_BROKER_HOST", "localhost")
os.environ.setdefault("MQTT_USER", "openapi-export")
os.environ.setdefault("MQTT_PASSWORD", "openapi-export")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OVMS_DATABASE_URL", "sqlite:///:memory:")


def _throwaway_public_key() -> str:
    """
    A structurally valid Ed25519 public key for the startup check.

    Importing main runs check_jwt_configuration(), which insists on a usable key —
    correctly so, since a server that boots without one fails every login later. The
    export only needs the import to succeed, and this key verifies nothing: it is
    generated here, never written anywhere, and discarded when the process exits.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode()


os.environ.setdefault("JWT_PUBLIC_KEY", _throwaway_public_key())

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Where the main server keeps the merged Karto documentation. The default assumes the
# two checkouts are siblings and the main one is called "PyOVMS"; neither is true
# for everyone, so it can be overridden by argument or environment:
#   python export_openapi.py ../PyOVMS/doc/karto-openapi.json
#   PYOVMS_OPENAPI_OUTPUT=/opt/PyOVMS/doc/karto-openapi.json python export_openapi.py
_ENV_OUTPUT = os.environ.get("PYOVMS_OPENAPI_OUTPUT")
DEFAULT_OUTPUT = (
    Path(_ENV_OUTPUT).expanduser()
    if _ENV_OUTPUT
    else Path(__file__).resolve().parent.parent / "PyOVMS" / "doc" / "karto-openapi.json"
)


def main() -> int:
    from main import app

    schema = app.openapi()

    paths = schema.get("paths", {})
    if not paths:
        print("Refusing to write an empty schema — no paths were generated.", file=sys.stderr)
        return 1

    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUTPUT.resolve()
    if not output.parent.is_dir():
        print(f"Output directory does not exist: {output.parent}", file=sys.stderr)
        print("Pass the path as an argument or set PYOVMS_OPENAPI_OUTPUT.", file=sys.stderr)
        return 1

    # Sorted keys and a trailing newline so a regenerated file produces a readable
    # diff instead of a reshuffled blob.
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"Wrote {len(paths)} paths and {len(schema.get('components', {}).get('schemas', {}))} "
          f"schemas to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
