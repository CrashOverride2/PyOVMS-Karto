"""
Regression tests for C-2: the render server must not expose the project root.

map_worker used to launch tests/server.py, which served the whole project directory with
`show_index=True`. app/config.py expects karto.env in exactly that directory, so

    curl http://localhost:8082/karto.env

returned SECRET_KEY_JWT plus both database URLs to any local account or co-located
container. Because that JWT secret is shared with the OVMS main server, reading it is a
full account takeover *there* — Karto was only the way in.

These tests start the real server against a throwaway directory layout and assert what
is and is not reachable.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO_ROOT / "app" / "render_server.py"

TOKEN = "test-token-abcdef"

STARTUP_TIMEOUT_SECONDS = 30


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str):
    """Return (status, body) and turn HTTP errors into a status instead of raising."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _stderr(process: subprocess.Popen) -> str:
    """Whatever the server managed to say before dying, for the failure message."""
    if process.stderr is None:
        return "<no stderr captured>"
    return process.stderr.read().decode(errors="replace").strip() or "<no output>"


@pytest.fixture(scope="module")
def render_server(tmp_path_factory):
    """
    Lay out a fake project root — including a karto.env holding a marker secret — and
    start the render server the way map_worker does.
    """
    root = tmp_path_factory.mktemp("fake_project_root")
    (root / "karto.env").write_text("SECRET_KEY_JWT=SUPER-SECRET-MARKER\n")

    template_dir = root / "map_templates"
    template_dir.mkdir()
    (template_dir / "template.html").write_text("<html><body>map</body></html>")
    (template_dir / "leaflet.css").write_text("/* css */")

    geojson_dir = root / "temp_render_files"
    geojson_dir.mkdir()
    (geojson_dir / "trip.json").write_text('{"type":"LineString","coordinates":[]}')

    pmtiles = root / "basemap.pmtiles"
    pmtiles.write_bytes(b"PMTILES-DATA")

    port = _free_port()
    env = {
        **os.environ,
        "KARTO_RENDER_PORT": str(port),
        "KARTO_RENDER_TOKEN": TOKEN,
        "KARTO_RENDER_GEOJSON_DIR": str(geojson_dir),
        "KARTO_RENDER_TEMPLATE_DIR": str(template_dir),
        "KARTO_RENDER_PMTILES": str(pmtiles),
    }
    process = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"render server exited with {process.returncode}: {_stderr(process)}")
        try:
            _get(f"{base}/{TOKEN}/template/template.html")
            break
        except Exception:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail(f"render server did not come up: {_stderr(process)}")

    yield base, root

    process.terminate()
    process.wait(timeout=10)


# --- what must be reachable ---------------------------------------------------------

def test_template_is_served(render_server):
    base, _ = render_server
    status, body = _get(f"{base}/{TOKEN}/template/template.html")
    assert status == 200 and b"map" in body


def test_template_assets_are_served(render_server):
    """The template references ./leaflet.css relatively, so its directory must work."""
    base, _ = render_server
    status, _body = _get(f"{base}/{TOKEN}/template/leaflet.css")
    assert status == 200


def test_geojson_is_served(render_server):
    base, _ = render_server
    status, body = _get(f"{base}/{TOKEN}/geojson/trip.json")
    assert status == 200 and b"LineString" in body


def test_pmtiles_is_served(render_server):
    base, _ = render_server
    status, body = _get(f"{base}/{TOKEN}/tiles.pmtiles")
    assert status == 200 and body == b"PMTILES-DATA"


# --- what must NOT be reachable -----------------------------------------------------

@pytest.mark.parametrize("path", [
    "/karto.env",
    "/.env",
    f"/{TOKEN}/karto.env",
    f"/{TOKEN}/template/../karto.env",
    f"/{TOKEN}/geojson/../karto.env",
    f"/{TOKEN}/geojson/../../karto.env",
])
def test_secrets_are_not_reachable(render_server, path):
    base, _ = render_server
    status, body = _get(f"{base}{path}")
    assert status != 200, f"{path} was served"
    assert b"SUPER-SECRET-MARKER" not in body


def test_project_root_is_not_listed(render_server):
    """show_index=True used to enumerate the whole project directory."""
    base, _ = render_server
    for path in ("/", f"/{TOKEN}/", f"/{TOKEN}/geojson/", f"/{TOKEN}/template/"):
        status, body = _get(f"{base}{path}")
        assert status != 200 or b"karto.env" not in body, f"{path} produced a directory listing"


def test_wrong_token_is_rejected(render_server):
    base, _ = render_server
    status, _body = _get(f"{base}/wrong-token/template/template.html")
    assert status == 404
