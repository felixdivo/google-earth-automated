"""Serve single Earth Studio frames over HTTP, from a pool of warm browsers.

    uv run earth serve
    uv run earth serve --workers 4 --port 9000
    curl -o frame.jpg 'http://localhost:8000/frame?lat=50.107&lon=8.663&alt=145'

A gunicorn fleet: N worker processes behind one listening socket, each keeping
one logged-in Earth Studio page open with a single scratch project loaded. A
request types its camera pose into the camera fields of the project that worker
already has open, takes a snapshot, and streams the JPEG straight back --
nothing is imported per request, so the import and the whole-scene rebuild it
triggers are paid once per worker, at startup.

Frames are captured with Earth Studio's own snapshot button rather than through
the render panel: same resolution (the project's, not the viewport's), half the
time, and no output folder -- which means no File System Access grant and none
of the native-dialog machinery `earth render` needs. That is also why the fleet
is always headless: nothing here ever raises a native dialog that a keystroke
would have to be aimed at a real window to answer.

Each worker is a process, not a tab, because one Chromium may hold a profile at
a time -- so every worker gets a throwaway copy of the login profile (see
copy_profile) and, where the container was given several GPUs, one of those to
itself (see gpu_pinning.py). Frame size is fixed for the life of the server: it
is the one thing a request cannot change without importing a new project. No
frame is ever written to disk -- snapshots are read out of the browser's
download and deleted -- and nothing is ever written under the repo.

How fast this is, how far --workers scales and what stops it: docs/performance.md.

Requires a logged-in session -- run `earth login` first.

Binds 127.0.0.1 by default -- there's no auth on this API, so the default keeps
it off the network entirely. Running through `./run.sh` (real Docker port
publishing, which routes through the container's bridge interface rather than
its loopback) needs `--host 0.0.0.0` for `--publish` to have anything to reach;
run.sh binds that published port to the *host's* own 127.0.0.1, so it stays
host-local either way. The devcontainer's own port forwarding proxies inside the
container's network namespace, so the default works there unchanged.
"""

import os
import shutil
import signal
import sys
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from http import HTTPStatus
from itertools import count
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

import typer
from gunicorn.app.base import BaseApplication
from gunicorn.arbiter import Arbiter
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from . import browser
from .build import build_still_esp, write_esp
from .gpu_pinning import fleet_gpus, pin_to_gpu
from .render import NotLoggedInError, Session

# The known ways a frame can fail -- Playwright interaction errors (a moved
# selector, a snapshot download that never arrives), the TimeoutError/
# RuntimeError render.py itself raises (a camera field that refused its value),
# and filesystem errors from staging the .esp or reading the download. Caught
# narrowly (rather than bare Exception) so a genuine bug still surfaces instead
# of quietly becoming a 500 on every request.
RENDER_ERRORS = (PlaywrightError, RuntimeError, TimeoutError, OSError)

# The one project every request re-poses. Nothing downstream depends on the
# name, so it is fixed and boring on purpose.
PROJECT_NAME = "earth-serve"

# Every frame this server will ever produce is this size: it comes from the
# imported project, and re-importing per request is exactly the cost the fleet
# exists to avoid.
FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080

# How long one snapshot may take before the request gives up and says so.
DEFAULT_FRAME_TIMEOUT_S = 180.0

# How long a worker may go without finishing a request before gunicorn decides
# it is wedged rather than slow, kills it and forks a replacement. Past the
# frame timeout by enough that a merely slow frame fails inside the request,
# where it can say what went wrong, instead of taking its browser down with it.
WORKER_TIMEOUT_S = DEFAULT_FRAME_TIMEOUT_S + 30.0

# gunicorn's hooks are plain callables with nowhere to hang state, so what the
# arbiter works out before it forks reaches the workers the only way it can: as
# module globals, carried across the fork. _session is the other direction --
# post_fork owns it, and the WSGI callable below has no handle on the worker
# object to reach it any other way.
_profile_root: Path
_gpus: list[Path] = []
_session: Session | None = None


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


def load_project(session: Session) -> None:
    """Import the scratch project this worker re-poses for the rest of its life.

    No warmup render: capturing with the snapshot button needs no output folder,
    so there is no File System Access grant to pay for up front and nothing to
    warm up. The .esp is deleted as soon as it has been read -- once the project
    is in the page, the file has done its job.

    A uniquely named temp file rather than a scratch directory: N workers import
    at once, none of them should be writing the same path, and a worker that is
    killed outright then leaves nothing behind to clean up. The project's name in
    the editor comes from settings.name, not the filename, so it stays
    PROJECT_NAME whatever the file is called.
    """
    esp = build_still_esp(
        0.0,
        0.0,
        1000.0,
        heading=0.0,
        tilt=45.0,
        name=PROJECT_NAME,
        width=FRAME_WIDTH,
        height=FRAME_HEIGHT,
    )
    handle, path = tempfile.mkstemp(prefix="earth-serve-", suffix=".esp")
    os.close(handle)
    esp_file = Path(path)
    try:
        write_esp(esp, esp_file)
        session.import_project(esp_file)
    finally:
        esp_file.unlink(missing_ok=True)


def copy_profile(dest: Path) -> None:
    """Copy the login profile for one worker to run on.

    Copies are why the fleet works at all: they carry the same logged-in Google
    session, and measured with up to 8 running at once, no worker was ever sent
    back to a sign-in page and the master profile's cookies came back untouched.

    Skips Default/Cache -- 1.3GB of the profile's 1.4GB, and nothing but map
    tiles for places already visited, which a server answering arbitrary poses
    barely reuses. (There is no way to *share* one cache between browsers:
    Chromium's Simple Cache rewrites its entry files in place, so a shared
    directory, or a hardlink farm, corrupts.) The Singleton* files are the lock
    tying a profile to one running browser, so a copy must not inherit them.

    A plain copytree: measured at ~90MB and ~0.1s per worker here, paid once at
    worker startup, so the copy-on-write tricks that would make it near-free on
    btrfs or xfs (os.copy_file_range(), or shelling out to `cp --reflink=auto`)
    buy nothing worth the complexity.
    """
    shutil.copytree(browser.PROFILE_DIR, dest, ignore=shutil.ignore_patterns("Cache", "Singleton*"))


# --- the HTTP surface: two GETs, no framework ---------------------------------


def application(environ: dict, start_response: Callable) -> list[bytes]:
    """The WSGI callable every gunicorn worker serves.

    Two routes do not justify a framework. They are also served one at a time
    per worker, by gunicorn's default sync worker: this process owns exactly one
    Chromium profile and one Earth Studio page (Playwright's sync API isn't
    thread-safe either), so its requests would serialize whatever it did.
    Concurrency is the fleet, not the worker.
    """
    path = environ.get("PATH_INFO", "")
    if path == "/health":
        # gunicorn's own bookkeeping is the readiness check: a worker is only
        # added to the accept loop once post_fork has handed it a warm browser,
        # so anything that answers this at all is ready to render.
        return _respond(start_response, HTTPStatus.OK, b"ok")
    if path == "/frame":
        return _frame(environ, start_response)
    return _respond(start_response, HTTPStatus.NOT_FOUND, f"no such endpoint: {path}".encode())


def _frame(environ: dict, start_response: Callable) -> list[bytes]:
    params = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        lat = _parse_float(params, "lat")
        lon = _parse_float(params, "lon")
        alt = _parse_float(params, "alt")
        heading = _parse_float(params, "heading", default=0.0)
        tilt = _parse_float(params, "tilt", default=45.0)
    except ValueError as e:
        return _respond(start_response, HTTPStatus.BAD_REQUEST, str(e).encode())

    assert _session is not None  # post_fork ran before this worker started accepting
    try:
        jpeg_bytes = _session.capture_pose(
            lat=lat,
            lon=lon,
            alt=alt,
            heading=heading,
            tilt=tilt,
            timeout_ms=int(DEFAULT_FRAME_TIMEOUT_S * 1000),
        )
    except RENDER_ERRORS as e:
        print(f"frame failed: {e}", flush=True)
        return _respond(
            start_response, HTTPStatus.INTERNAL_SERVER_ERROR, f"frame failed: {e}".encode()
        )

    return _respond(start_response, HTTPStatus.OK, jpeg_bytes, content_type="image/jpeg")


def _respond(
    start_response: Callable,
    status: HTTPStatus,
    body: bytes,
    *,
    content_type: str = "text/plain; charset=utf-8",
) -> list[bytes]:
    start_response(
        f"{status.value} {status.phrase}",
        [("Content-Type", content_type), ("Content-Length", str(len(body)))],
    )
    return [body]


# --- the worker lifecycle, as gunicorn hooks ----------------------------------


def pre_fork(server: Arbiter, worker: object) -> None:
    """Number the worker about to be forked, in the arbiter, where the number
    outlives it.

    gunicorn identifies a worker by pid and by an ever-increasing age, neither of
    which a respawn preserves -- but a replacement has to come back to the same
    GPU, or a fleet that is rebuilt often slowly bunches up on one card. So each
    worker takes the lowest slot no live worker holds, and a dead worker's slot
    is free again by the time its replacement is forked.
    """
    taken = {getattr(w, "earth_slot", -1) for w in server.WORKERS.values()}
    worker.earth_slot = next(i for i in count() if i not in taken)


def post_fork(server: Arbiter, worker: object) -> None:
    """Turn a freshly forked worker into a warm, logged-in browser.

    Everything expensive happens here, once per worker: the profile copy, the
    GPU pin, the browser launch, the project import. gunicorn only starts the
    worker's accept loop once this returns, so a request can never land on a
    half-built worker.
    """
    # The arbiter's SIGCHLD handler came across the fork and would reap this
    # worker's own children -- the mount helpers pin_to_gpu runs, Playwright's
    # node driver -- out from under it. gunicorn resets it in init_process(),
    # which runs after this hook, so it has to go now.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    profile = _profile_root / f"profile-{worker.earth_slot}"
    # A worker killed outright (a browser still wedged past WORKER_TIMEOUT_S)
    # never got to run worker_exit, so its slot's copy may still be lying about.
    shutil.rmtree(profile, ignore_errors=True)
    copy_profile(profile)
    worker.earth_profile = profile

    if _gpus:
        # Round-robin by slot, so the fleet stays evenly spread however often it
        # is rebuilt.
        pin_to_gpu(_gpus[worker.earth_slot % len(_gpus)])

    # An ExitStack rather than a `with`: the browser has to outlive this hook and
    # be closed by a different one (worker_exit), and the stack is what carries
    # the context-manager protocol across that gap.
    global _session
    stack = ExitStack()
    worker.earth_stack = stack
    try:
        playwright = stack.enter_context(sync_playwright())
        _session = stack.enter_context(Session(playwright, profile_dir=profile, headless=True))
        print(f"Loading a {FRAME_WIDTH}x{FRAME_HEIGHT} project...", flush=True)
        load_project(_session)
    except NotLoggedInError as e:
        stack.close()
        print(f"error: {e}", file=sys.stderr, flush=True)
        # The one exit code the arbiter reads as "stop the whole fleet". Any
        # other, and it would respawn this worker forever against a login that
        # is not coming back.
        sys.exit(Arbiter.WORKER_BOOT_ERROR)
    except BaseException:  # including KeyboardInterrupt during the browser launch
        stack.close()
        raise


def worker_exit(server: Arbiter, worker: object) -> None:
    """Close this worker's browser and delete the profile copy it ran on.

    Runs in the worker, from gunicorn's own `finally`, so it covers a graceful
    stop, a crash, and the SIGABRT the arbiter sends a timed-out worker alike --
    everything except an outright SIGKILL, which is what post_fork's rmtree is
    there to clean up after.
    """
    stack = getattr(worker, "earth_stack", None)
    if stack is not None:
        stack.close()
    profile = getattr(worker, "earth_profile", None)
    if profile is not None:
        shutil.rmtree(profile, ignore_errors=True)


def when_ready(server: Arbiter) -> None:
    print(
        "GET /frame?lat=..&lon=..&alt=..&heading=..&tilt=.. "
        f"({FRAME_WIDTH}x{FRAME_HEIGHT}, {server.cfg.workers} worker(s))",
        flush=True,
    )


def on_exit(server: Arbiter) -> None:
    # Each worker deletes its own copy on the way out; this is the backstop for
    # the ones that were killed before they could.
    shutil.rmtree(_profile_root, ignore_errors=True)
    print("Server stopped.", flush=True)


class _Fleet(BaseApplication):
    """gunicorn as a library, configured in code rather than from a file.

    Everything `earth serve` needs from a process supervisor -- forking N
    workers, handing them one shared listening socket, restarting one that dies
    or wedges -- is gunicorn's prefork model. The hooks above are the only part
    of it that is specific to this app.
    """

    def __init__(self, options: dict):
        self.options = options
        super().__init__()

    def load_config(self) -> None:
        for key, value in self.options.items():
            self.cfg.set(key, value)

    def load(self) -> Callable:
        return application


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
    workers: Annotated[
        int,
        typer.Option(
            min=1,
            help=(
                "How many browsers to answer requests with, each on its own copy of the "
                "login profile. Scales linearly to about 4; past that Google's map-tile "
                "streaming, not this machine, is the limit -- see docs/performance.md."
            ),
        ),
    ] = 1,
) -> None:
    """Serve single Earth Studio frames over HTTP from a pool of warm browsers."""
    global _profile_root, _gpus
    _profile_root = Path(tempfile.mkdtemp(prefix="earth-serve-"))
    _gpus = fleet_gpus(workers)

    spread = f" across {len(_gpus)} GPUs" if _gpus else ""
    print(
        f"Starting {workers} worker(s){spread}, each on its own copy of the profile...",
        flush=True,
    )

    # From here on the arbiter owns SIGINT/SIGTERM -- it installs its own
    # handlers over shutdown.py's, and takes the fleet down through worker_exit
    # instead. Everything shutdown.py guards against still applies inside a
    # worker, which is why the browser is closed from a context manager there.
    _Fleet(
        {
            "bind": f"{host}:{port}",
            "workers": workers,
            "timeout": int(WORKER_TIMEOUT_S),
            # Deliberately no max_requests: recycling a worker would throw away
            # the warm browser that makes this server fast.
            "accesslog": "-",
            "pre_fork": pre_fork,
            "post_fork": post_fork,
            "worker_exit": worker_exit,
            "when_ready": when_ready,
            "on_exit": on_exit,
        }
    ).run()
