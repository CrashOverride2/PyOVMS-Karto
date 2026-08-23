"""
Minimal static file server for the Playwright map renderer.

Chromium cannot load the renderer over file:// (PMTiles range requests and the
fetch() for the trip GeoJSON both fail there), so the worker starts a tiny HTTP
server on the loopback interface and points the browser at it.

The previous implementation lived in tests/server.py and served the **entire project
root** with `show_index=True`. That directory is where app/config.py expects
`karto.env`, so `curl http://localhost:8082/karto.env` returned SECRET_KEY_JWT,
DATABASE_URL and OVMS_DATABASE_URL to any local user or co-located container — and
since the JWT secret is shared with the OVMS main server, that is a full account
takeover there, not just a Karto problem.

This version exposes exactly three things, each under its own route:

    /<token>/template/…      app/map_templates/ (template.html + leaflet/pmtiles assets)
    /<token>/geojson/…       the per-trip GeoJSON the worker just wrote
    /<token>/tiles.pmtiles   the configured PMTiles basemap, if any

Nothing else is reachable, directory listings are off, and the random path token means
a process that merely guesses the port still cannot read anything.
"""

import os
import sys
from pathlib import Path

from aiohttp import web

TOKEN_BYTES = 16


def build_app(token: str, geojson_dir: Path, template_dir: Path, pmtiles_file: Path | None) -> web.Application:
    app = web.Application()

    # show_index=False: a listing would enumerate every trip id that has been rendered.
    # follow_symlinks stays at its default (False) so a symlink dropped into either
    # directory cannot be used to read outside of it.
    app.router.add_static(f"/{token}/template", template_dir, show_index=False)
    app.router.add_static(f"/{token}/geojson", geojson_dir, show_index=False)

    if pmtiles_file is not None:
        async def serve_pmtiles(_request: web.Request) -> web.FileResponse:
            # A single explicit file, not its parent directory: the basemap may live
            # anywhere on disk and we must not expose whatever sits next to it.
            return web.FileResponse(pmtiles_file)

        app.router.add_get(f"/{token}/tiles.pmtiles", serve_pmtiles)

    return app


def main() -> None:
    try:
        port = int(os.environ["KARTO_RENDER_PORT"])
        token = os.environ["KARTO_RENDER_TOKEN"]
        geojson_dir = Path(os.environ["KARTO_RENDER_GEOJSON_DIR"])
        template_dir = Path(os.environ["KARTO_RENDER_TEMPLATE_DIR"])
    except KeyError as exc:
        print(f"render_server: missing required environment variable {exc}", file=sys.stderr)
        raise SystemExit(2)

    pmtiles_env = os.environ.get("KARTO_RENDER_PMTILES") or None
    pmtiles_file = Path(pmtiles_env) if pmtiles_env else None

    app = build_app(token, geojson_dir, template_dir, pmtiles_file)
    # Loopback only. This server has no authentication beyond the path token and must
    # never be reachable from another host.
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
