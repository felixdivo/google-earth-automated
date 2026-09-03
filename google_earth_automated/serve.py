"""Serve single Earth Studio frames over HTTP, from one warm browser session.

    uv run earth serve
    uv run earth serve --port 9000 --headless

Starts a tiny HTTP server that keeps one logged-in Earth Studio page open,
with a single scratch project loaded in it, and answers each request by typing
the request's camera pose into that project's camera fields and taking a
snapshot -- then streams the JPEG straight back.

Two things make that fast. Nothing is imported per request, so the import and
the whole-scene rebuild it triggers are paid once at startup along with the
browser launch and the SPA load. And frames are captured with Earth Studio's
own snapshot button rather than through the render panel: same resolution
(the project's, not the viewport's), half the time, and no output folder --
which means no File System Access grant and none of the native-dialog
machinery `earth render` needs. Measured on this host: startup ~8s once the
browser is up, and a request 3-6s of which most is Earth Studio drawing.

Frame size is fixed for the life of the server (--width/--height), because it
is the one thing a request cannot change without importing a new project.

The server writes no frames to disk at all -- snapshots are read out of the
browser's download and deleted -- and nothing is ever written under the repo.
It's also single-threaded on purpose: there is exactly one Chromium profile
and one Earth Studio page (Playwright's sync API isn't thread-safe either), so
requests serialize behind the one worker regardless.

Requires a logged-in session -- run `earth login` first.

    curl -o frame.jpg \\
        'http://localhost:8000/frame?lat=50.1070&lon=8.6633&alt=145&heading=180&tilt=70'

Binds 127.0.0.1 by default -- there's no auth on this API, so the default
keeps it off the network entirely. Running through `./run.sh` (real Docker
port publishing, which routes through the container's bridge interface
rather than its loopback) needs `--host 0.0.0.0` for `--publish` to have
anything to reach; `run.sh` binds that published port to the *host's* own
127.0.0.1 (see run.sh), so it stays host-local either way. The devcontainer's
own port forwarding proxies inside the container's network namespace, so the
127.0.0.1 default works there unchanged.
"""

import shutil
import sys
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import typer
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .build import build_still_esp, write_esp
from .render import NotLoggedInError, Session

# The known ways a frame can fail -- Playwright interaction errors (a moved
# selector, a snapshot download that never arrives), the TimeoutError/
# RuntimeError render.py itself raises (a camera field that refused its value),
# and filesystem errors from staging the .esp or reading the download. Caught
# narrowly (rather than bare Exception) so a genuine bug still surfaces instead
# of quietly becoming a 500 on every request.
RENDER_ERRORS = (PlaywrightError, RuntimeError, TimeoutError, OSError)

# The scratch .esp exists only long enough for the browser's file chooser to
# read it at startup -- frames never touch the disk, so nothing else lives here
# and nothing is written under the repo.
STAGING_DIR = Path(tempfile.gettempdir()) / "earth-serve-frames"

# The one project every request re-poses. Nothing downstream depends on the
# name, so it is fixed and boring on purpose.
PROJECT_NAME = "earth-serve"

DEFAULT_FRAME_TIMEOUT_S = 180.0


def _parse_float(params: dict, name: str, *, default: float | None = None) -> float:
    values = params.get(name)
    if not values:
        if default is not None:
            return default
        raise ValueError(f"missing required parameter '{name}'")
    try:
        return float(values[0])
    except ValueError as e:
        raise ValueError(f"parameter '{name}' must be a number, got {values[0]!r}") from e


def load_project(session: Session, *, width: int, height: int) -> None:
    """Import the scratch project the server re-poses for the rest of its life.

    No warmup render: capturing with the snapshot button needs no output folder,
    so there is no File System Access grant to pay for up front and nothing to
    warm up. The .esp is deleted as soon as it has been read -- once the project
    is in the page, the file has done its job.
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    esp_file = STAGING_DIR / f"{PROJECT_NAME}.esp"
    esp = build_still_esp(
        0.0, 0.0, 1000.0, heading=0.0, tilt=45.0, name=PROJECT_NAME, width=width, height=height
    )
    write_esp(esp, esp_file)
    try:
        session.import_project(esp_file)
    finally:
        esp_file.unlink(missing_ok=True)


class _FrameRequestHandler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler builds one instance per request, so these are set
    # as class attributes before serve_forever() to reach every instance.
    session: Session
    frame_timeout_s: float

    def log_message(self, log_format: str, *args) -> None:
        print(f"{self.address_string()} - {log_format % args}", flush=True)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._respond_text(HTTPStatus.OK, "ok")
        elif path == "/frame":
            self._handle_frame()
        else:
            self._respond_text(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")

    def _handle_frame(self) -> None:
        params = parse_qs(urlsplit(self.path).query)
        try:
            lat = _parse_float(params, "lat")
            lon = _parse_float(params, "lon")
            alt = _parse_float(params, "alt")
            heading = _parse_float(params, "heading", default=0.0)
            tilt = _parse_float(params, "tilt", default=45.0)
        except ValueError as e:
            self._respond_text(HTTPStatus.BAD_REQUEST, str(e))
            return

        try:
            jpeg_bytes = self.session.capture_pose(
                lat=lat,
                lon=lon,
                alt=alt,
                heading=heading,
                tilt=tilt,
                timeout_ms=int(self.frame_timeout_s * 1000),
            )
        except RENDER_ERRORS as e:
            self.log_message("frame failed: %s", e)
            self._respond_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"frame failed: {e}")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg_bytes)))
        self.end_headers()
        self.wfile.write(jpeg_bytes)

    def _respond_text(self, status: HTTPStatus, message: str) -> None:
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def command(
    host: Annotated[
        str,
        typer.Option(
            help=(
                "Interface to bind to. Defaults to loopback-only since there's no auth on "
                "this API; pass 0.0.0.0 to make it reachable from outside the container "
                "(needed for ./run.sh's Docker port publishing -- see serve.py's docstring)."
            ),
        ),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    headless: Annotated[
        bool,
        typer.Option(
            help="Run the browser headless (default: headed, until selectors are verified)."
        ),
    ] = False,
    width: Annotated[int, typer.Option(help="Frame width in pixels, for every frame.")] = 1920,
    height: Annotated[int, typer.Option(help="Frame height in pixels, for every frame.")] = 1080,
    frame_timeout: Annotated[
        float, typer.Option(help="How long one frame may take, in seconds.")
    ] = DEFAULT_FRAME_TIMEOUT_S,
) -> None:
    """Serve single Earth Studio frames over HTTP from one warm session."""
    # Clear any leftovers from an earlier, uncleanly stopped server.
    shutil.rmtree(STAGING_DIR, ignore_errors=True)

    with sync_playwright() as playwright:
        try:
            session = Session(playwright, headless=headless)
        except NotLoggedInError as e:
            sys.exit(f"error: {e}")

        # Everything past this point owns a live browser and a scratch
        # directory, and any of it can be cut short by Ctrl-C (or SIGTERM,
        # e.g. `docker stop`) -- during the warmup, while idle, or mid-render
        # inside a request. All three exit through this one finally.
        try:
            print(f"Loading a {width}x{height} project...", flush=True)
            try:
                load_project(session, width=width, height=height)
            except RENDER_ERRORS as e:
                sys.exit(f"error: could not load the scratch project: {e}")

            _FrameRequestHandler.session = session
            _FrameRequestHandler.frame_timeout_s = frame_timeout

            # `with` closes the listening socket on the way out, so the port is
            # free again the moment the server stops.
            with HTTPServer((host, port), _FrameRequestHandler) as server:
                print(
                    f"Listening on http://{host}:{port} -- "
                    "GET /frame?lat=..&lon=..&alt=..&heading=..&tilt=.. "
                    f"({width}x{height})",
                    flush=True,
                )
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass  # the signal handler already said so
        finally:
            session.close()
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
            print("Server stopped.", flush=True)
