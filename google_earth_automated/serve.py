"""Serve single Earth Studio frames over HTTP, from one warm browser session.

    uv run earth serve
    uv run earth serve --port 9000 --headless

Starts a tiny HTTP server that keeps one logged-in Earth Studio page open and
answers each request by rendering exactly one frame -- a duration=0 .esp built
from the request's camera pose, imported into that same warm page and
rendered as an Image Sequence of one -- then streams the JPEG straight back.
All the fixed cost `earth render` pays per invocation (browser launch, SPA
load, the File System Access folder grant) is paid once at startup instead,
so a request is just the render itself.

The server keeps no state between requests: each frame's scratch project file
and output folder are deleted the moment its bytes are read, and nothing is
ever written under the repo (see STAGING_DIR below). It's also
single-threaded on purpose -- there is exactly one Chromium profile and one
Earth Studio page (Playwright's sync API isn't thread-safe either), so
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
from itertools import count
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import typer
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .build import build_still_esp, write_esp
from .render import NotLoggedInError, Session

# The known ways a render can fail -- Playwright interaction errors, the
# TimeoutError/RuntimeError render.py itself raises, and filesystem errors
# from staging the .esp/reading the frame. Caught narrowly (rather than bare
# Exception) so a genuine bug still surfaces instead of quietly becoming a 500
# on every request.
RENDER_ERRORS = (PlaywrightError, RuntimeError, TimeoutError, OSError)

# Frames are scratch, not output -- this path is fixed (not a fresh
# mkdtemp()) so Chromium's File System Access grant for it survives a server
# restart, letting a later `earth serve` skip the native folder-picker dialog
# and stay fast from its very first request. Lives outside the repo and
# outside out/: nothing this command produces is meant to be kept.
STAGING_DIR = Path(tempfile.gettempdir()) / "earth-serve-frames"

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


def _parse_int(params: dict, name: str, default: int) -> int:
    values = params.get(name)
    if not values:
        return default
    try:
        return int(values[0])
    except ValueError as e:
        raise ValueError(f"parameter '{name}' must be an integer, got {values[0]!r}") from e


_frame_ids = count()


def render_frame(
    session: Session,
    lat: float,
    lon: float,
    alt: float,
    *,
    heading: float,
    tilt: float,
    width: int,
    height: int,
    timeout_s: float,
) -> bytes:
    """Render one still frame and return its JPEG bytes.

    Cleans up its scratch project file and output folder before returning,
    on both success and failure -- nothing from this call outlives it on
    disk, and the next call starts from a clean STAGING_DIR.
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    name = f"frame_{next(_frame_ids)}"
    esp_file = STAGING_DIR / f"{name}.esp"
    frame_dir = STAGING_DIR / name  # Earth Studio nests output under a project-name subfolder

    esp = build_still_esp(
        lat, lon, alt, heading=heading, tilt=tilt, name=name, width=width, height=height
    )
    write_esp(esp, esp_file)

    try:
        # The File System Access grant is for STAGING_DIR itself (the folder
        # picker can only target a directory that exists before the render),
        # so wait_for_frames necessarily watches STAGING_DIR, not frame_dir --
        # which is exactly why leftovers from an earlier request must not
        # still be sitting there when this call starts.
        session.render_esp(
            esp_file,
            STAGING_DIR,
            expected_frame_count=1,
            timeout_ms=int(timeout_s * 1000),
            progress=lambda _message: None,  # stay quiet -- one request among many
        )
        frames = sorted(frame_dir.rglob("*.jp*g"))
        if not frames:
            raise RuntimeError(f"render reported done but no frame appeared in {frame_dir}")
        return frames[0].read_bytes()
    finally:
        esp_file.unlink(missing_ok=True)
        shutil.rmtree(frame_dir, ignore_errors=True)


class _FrameRequestHandler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler builds one instance per request, so these are set
    # as class attributes before serve_forever() to reach every instance.
    session: Session
    default_width: int
    default_height: int
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
            width = _parse_int(params, "width", self.default_width)
            height = _parse_int(params, "height", self.default_height)
        except ValueError as e:
            self._respond_text(HTTPStatus.BAD_REQUEST, str(e))
            return

        try:
            jpeg_bytes = render_frame(
                self.session,
                lat,
                lon,
                alt,
                heading=heading,
                tilt=tilt,
                width=width,
                height=height,
                timeout_s=self.frame_timeout_s,
            )
        except RENDER_ERRORS as e:
            self.log_message("render failed: %s", e)
            self._respond_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"render failed: {e}")
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
    width: Annotated[int, typer.Option(help="Default frame width in pixels.")] = 1920,
    height: Annotated[int, typer.Option(help="Default frame height in pixels.")] = 1080,
    frame_timeout: Annotated[
        float, typer.Option(help="Per-frame render timeout, in seconds.")
    ] = DEFAULT_FRAME_TIMEOUT_S,
) -> None:
    """Serve single rendered Earth Studio frames over HTTP from one warm session."""
    # Clear any leftovers from an earlier, uncleanly stopped server -- render_frame
    # relies on STAGING_DIR starting empty (see its docstring).
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
            print("Warming up: rendering one throwaway frame...", flush=True)
            try:
                render_frame(
                    session,
                    lat=0.0,
                    lon=0.0,
                    alt=1000.0,
                    heading=0.0,
                    tilt=45.0,
                    width=width,
                    height=height,
                    timeout_s=frame_timeout,
                )
            except RENDER_ERRORS as e:
                sys.exit(f"error: warmup render failed: {e}")

            _FrameRequestHandler.session = session
            _FrameRequestHandler.default_width = width
            _FrameRequestHandler.default_height = height
            _FrameRequestHandler.frame_timeout_s = frame_timeout

            # `with` closes the listening socket on the way out, so the port is
            # free again the moment the server stops.
            with HTTPServer((host, port), _FrameRequestHandler) as server:
                print(
                    f"Listening on http://{host}:{port} -- "
                    "GET /frame?lat=..&lon=..&alt=..&heading=..&tilt=..",
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
