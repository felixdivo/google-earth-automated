"""Serve single Earth Studio frames over HTTP, from a pool of warm browsers.

    uv run earth serve
    uv run earth serve --workers 4
    uv run earth serve --port 9000 --headless

`earth serve` runs a fleet: N worker processes, each keeping one logged-in Earth
Studio page open with a single scratch project loaded, behind one dispatcher on
the public port. A request types its camera pose into the camera fields of the
project that worker already has open, takes a snapshot, and streams the JPEG
straight back.

Three things make that fast. Nothing is imported per request, so the import and
the whole-scene rebuild it triggers are paid once per worker at startup. Frames
are captured with Earth Studio's own snapshot button rather than through the
render panel: same resolution (the project's, not the viewport's), half the
time, and no output folder -- which means no File System Access grant and none
of the native-dialog machinery `earth render` needs. And requests run N at a
time instead of one, because a frame is mostly Earth Studio drawing on the GPU
and one browser leaves the machine idle. Measured on this host at 1920x1080,
three frames per worker: 0.10 frames/s at --workers 1 (median 4.6s per frame),
0.18 at 2, 0.31 at 4, 0.44 at 8 (median 9.5s, GPU pegged at ~85%). Past 4 the
extra throughput is bought with latency.

Each worker is a process, not a tab, because one Chromium may hold a profile at
a time: every worker gets a throwaway copy of .auth/chrome-profile (~90MB --
Default/Cache, 1.3GB of map tiles for places already visited, is skipped) in a
temp dir that is deleted on the way out. Copies are why the fleet works: they
carry the same logged-in Google session, and measured up to 8 at once, no worker
was ever sent back to a sign-in page and the master profile's cookies came back
untouched. `earth login` still writes only the real profile.

Frame size is fixed for the life of the server (--width/--height), because it is
the one thing a request cannot change without importing a new project.

The server writes no frames to disk at all -- snapshots are read out of the
browser's download and deleted -- and nothing is ever written under the repo.

Requires a logged-in session -- run `earth login` first.

    curl -o frame.jpg \\
        'http://localhost:8000/frame?lat=50.1070&lon=8.6633&alt=145&heading=180&tilt=70'

Binds 127.0.0.1 by default -- there's no auth on this API, so the default keeps
it off the network entirely. Running through `./run.sh` (real Docker port
publishing, which routes through the container's bridge interface rather than
its loopback) needs `--host 0.0.0.0` for `--publish` to have anything to reach;
`run.sh` binds that published port to the *host's* own 127.0.0.1 (see run.sh),
so it stays host-local either way. The devcontainer's own port forwarding
proxies inside the container's network namespace, so the 127.0.0.1 default works
there unchanged. Workers themselves always listen on loopback only.
"""

import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import typer
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .browser import PROFILE_DIR
from .build import build_still_esp, write_esp
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

DEFAULT_FRAME_TIMEOUT_S = 180.0

# How long a worker gets to launch its browser, load the SPA and import the
# scratch project before the fleet gives up on it. Generous: it covers a cold
# Chromium start on a busy machine, and it is only ever waited out on failure.
WORKER_STARTUP_TIMEOUT_S = 300.0

# How long the whole fleet gets to exit before its workers are killed outright.
# Comfortably inside shutdown.py's GRACE_PERIOD_S, so the profile copies still
# get deleted rather than the parent being force-exited on top of them.
FLEET_SHUTDOWN_S = 4.0

# How long a request waits for a worker to come free before giving up with 503.
# Long, because "all workers busy" is the normal state under load and a caller
# would rather wait than retry -- the queue is the backpressure.
WORKER_WAIT_TIMEOUT_S = 300.0


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
        0.0, 0.0, 1000.0, heading=0.0, tilt=45.0, name=PROJECT_NAME, width=width, height=height
    )
    handle, path = tempfile.mkstemp(prefix="earth-serve-", suffix=".esp")
    os.close(handle)
    esp_file = Path(path)
    try:
        write_esp(esp, esp_file)
        session.import_project(esp_file)
    finally:
        esp_file.unlink(missing_ok=True)


# --- the worker: one browser, one project, one frame at a time ----------------


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


def _serve_worker(
    *, host: str, port: int, headless: bool, width: int, height: int, frame_timeout: float
) -> None:
    """One browser answering one frame at a time, on its own port.

    Single-threaded on purpose: this process owns exactly one Chromium profile
    and one Earth Studio page (Playwright's sync API isn't thread-safe either),
    so its requests serialize no matter what. Concurrency comes from running
    several of these -- see _serve_fleet().
    """
    with sync_playwright() as playwright:
        try:
            session = Session(playwright, headless=headless)
        except NotLoggedInError as e:
            sys.exit(f"error: {e}")

        # Everything past this point owns a live browser, and any of it can be
        # cut short by Ctrl-C (or SIGTERM, e.g. `docker stop`) -- during the
        # warmup, while idle, or mid-render inside a request. All three exit
        # through this one finally.
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
                print(f"Worker ready on http://{host}:{port}", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass  # the signal handler already said so
        finally:
            session.close()


# --- the fleet: N workers behind one port -------------------------------------


def copy_profile(dest: Path) -> None:
    """Copy the login profile for one worker to run on.

    Skips Default/Cache -- 1.3GB of the profile's 1.4GB, and nothing but map
    tiles for places already visited, which a server answering arbitrary poses
    barely reuses. (There is no way to *share* one cache between browsers:
    Chromium's Simple Cache rewrites its entry files in place, so a shared
    directory, or a hardlink farm, corrupts.) The Singleton* files are the lock
    tying a profile to one running browser, so a copy must not inherit them.

    Shells out to `cp` for --reflink=auto: a plain copy on ext4, near-free on a
    CoW filesystem. Measured at ~90MB and ~0.1s per worker here.
    """
    dest.mkdir(parents=True)
    top = [
        entry
        for entry in PROFILE_DIR.iterdir()
        if entry.name != "Default" and not entry.name.startswith("Singleton")
    ]
    _copy_into(top, dest)
    default = PROFILE_DIR / "Default"
    if default.is_dir():
        (dest / "Default").mkdir()
        _copy_into([e for e in default.iterdir() if e.name != "Cache"], dest / "Default")


def _copy_into(sources: list[Path], dest: Path) -> None:
    """`cp` has no --exclude, so the entries to keep are named explicitly, in
    batches small enough to stay well clear of the argument limit."""
    for start in range(0, len(sources), 200):
        subprocess.run(
            [
                "cp",
                "-a",
                "--reflink=auto",
                *[str(s) for s in sources[start : start + 200]],
                str(dest),
            ],
            check=True,
        )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Worker:
    """One worker process: its own profile copy, its own port, its own browser."""

    def __init__(self, index: int, profile_dir: Path, config: dict):
        self.index = index
        self.profile_dir = profile_dir
        self.config = config
        self.port = _free_port()
        self.process = self._spawn()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _spawn(self) -> subprocess.Popen:
        # -m rather than the `earth` console script, so this works the same
        # whether the parent was started as `uv run earth`, `python -m ...`, or
        # from an installed entry point.
        command = [
            sys.executable,
            "-m",
            "google_earth_automated",
            "serve",
            "--worker",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--width",
            str(self.config["width"]),
            "--height",
            str(self.config["height"]),
            "--frame-timeout",
            str(self.config["frame_timeout"]),
        ]
        if self.config["headless"]:
            command.append("--headless")
        process = subprocess.Popen(
            command,
            env={**os.environ, "EARTH_PROFILE_DIR": str(self.profile_dir)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._relay_output, args=(process,), daemon=True).start()
        return process

    def _relay_output(self, process: subprocess.Popen) -> None:
        """Tag a worker's own output so N of them stay readable in one terminal."""
        for line in process.stdout:
            print(f"[worker {self.index}] {line.rstrip()}", flush=True)

    def wait_until_ready(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False  # died on the way up -- its own output says why
            try:
                with urlopen(f"{self.url}/health", timeout=5) as response:
                    if response.status == HTTPStatus.OK:
                        return True
            except (URLError, HTTPError, OSError):
                pass  # not listening yet
            time.sleep(0.5)
        return False

    def terminate(self) -> None:
        """Ask this worker to shut down; it closes its own browser on the way."""
        if self.process.poll() is None:
            self.process.terminate()

    def wait_until_gone(self, deadline: float) -> None:
        """Wait for it to go, then insist. The deadline is shared across the
        fleet rather than per worker: shutdown.py force-exits the whole process
        after GRACE_PERIOD_S, so N workers waited out one after another would
        never finish cleaning up."""
        try:
            self.process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.process.kill()

    def stop(self) -> None:
        self.terminate()
        self.wait_until_gone(time.monotonic() + 10)
        shutil.rmtree(self.profile_dir, ignore_errors=True)


class WorkerPool:
    """The fleet, and which of its workers are free.

    A worker handles one frame at a time, so the pool hands each request an idle
    worker and takes it back afterwards; a request that finds none waits on the
    queue, which is where backpressure comes from.
    """

    def __init__(self, root: Path, count: int, config: dict):
        self.root = root
        self.count = count
        self.config = config
        self._idle: queue.Queue[Worker] = queue.Queue()
        self._workers: dict[int, Worker] = {}
        self._lock = threading.Lock()

    @property
    def ready(self) -> int:
        with self._lock:
            return len(self._workers)

    def start(self) -> None:
        """Bring every worker up, all at once.

        Concurrently, because a worker spends its startup waiting on a browser
        launch and an import: N workers cost about as long as one, not N times.
        """
        results: dict[int, Worker | None] = {}
        threads = [
            threading.Thread(target=self._start_one, args=(i, results), daemon=True)
            for i in range(self.count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for index in sorted(results):
            worker = results[index]
            if worker is None:
                continue
            with self._lock:
                self._workers[index] = worker
            self._idle.put(worker)

        if not self._workers:
            raise RuntimeError("no worker came up -- see the worker output above")
        if len(self._workers) < self.count:
            print(
                f"warning: only {len(self._workers)}/{self.count} workers came up",
                flush=True,
            )

    def _start_one(self, index: int, results: dict) -> None:
        # The copy belongs to bringing a worker up, not to starting the fleet:
        # a retired worker takes its copy with it (Worker.stop), so a
        # replacement needs a fresh one or it would launch on an empty profile
        # and be turned away at the login check.
        profile_dir = self.root / f"profile-{index}"
        shutil.rmtree(profile_dir, ignore_errors=True)
        copy_profile(profile_dir)
        worker = Worker(index, profile_dir, self.config)
        if worker.wait_until_ready(WORKER_STARTUP_TIMEOUT_S):
            results[index] = worker
        else:
            worker.stop()
            results[index] = None

    def acquire(self, timeout_s: float) -> Worker | None:
        try:
            return self._idle.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def release(self, worker: Worker) -> None:
        self._idle.put(worker)

    def replace(self, worker: Worker) -> None:
        """Retire a worker that stopped answering and bring up a fresh one.

        A wedged or crashed browser is a worker-shaped problem, not a fleet-shaped
        one: the others keep serving while this one is rebuilt in the background,
        and it rejoins the pool only once its /health answers.
        """
        with self._lock:
            self._workers.pop(worker.index, None)
        worker.stop()
        print(f"[worker {worker.index}] stopped answering -- restarting it", flush=True)
        threading.Thread(target=self._restart, args=(worker.index,), daemon=True).start()

    def _restart(self, index: int) -> None:
        results: dict[int, Worker | None] = {}
        self._start_one(index, results)
        replacement = results.get(index)
        if replacement is None:
            print(f"[worker {index}] could not be restarted", flush=True)
            return
        with self._lock:
            self._workers[index] = replacement
        self._idle.put(replacement)
        print(f"[worker {index}] back in the pool", flush=True)

    def close(self) -> None:
        """Take the whole fleet down, fast enough to still tidy up afterwards.

        Everyone is signalled first and waited for second, against one shared
        deadline -- terminating and waiting worker by worker takes longer than
        the grace period a Ctrl-C or `docker stop` allows, and the caller would
        then be killed before it could delete the profile copies.
        """
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.terminate()
        deadline = time.monotonic() + FLEET_SHUTDOWN_S
        for worker in workers:
            worker.wait_until_gone(deadline)


class _DispatchHandler(BaseHTTPRequestHandler):
    pool: WorkerPool
    frame_timeout_s: float

    def log_message(self, log_format: str, *args) -> None:
        print(f"{self.address_string()} - {log_format % args}", flush=True)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            ready = self.pool.ready
            status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
            self._respond(status, f"ok - {ready}/{self.pool.count} workers ready".encode())
        elif path == "/frame":
            self._dispatch_frame()
        else:
            self._respond(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}".encode())

    def _dispatch_frame(self) -> None:
        worker = self.pool.acquire(WORKER_WAIT_TIMEOUT_S)
        if worker is None:
            self._respond(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"all {self.pool.count} workers busy for {WORKER_WAIT_TIMEOUT_S:.0f}s".encode(),
            )
            return

        try:
            # A little longer than the worker's own frame timeout, so a slow
            # frame times out where it can say what went wrong rather than here.
            with urlopen(f"{worker.url}{self.path}", timeout=self.frame_timeout_s + 30) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "image/jpeg")
            self._respond(HTTPStatus.OK, body, content_type=content_type)
            self.pool.release(worker)
        except HTTPError as e:
            # The worker answered, it just said no (a bad pose, a failed frame):
            # pass its own status and message through, and keep the worker.
            self._respond(HTTPStatus(e.code), e.read(), content_type="text/plain; charset=utf-8")
            self.pool.release(worker)
        except (URLError, OSError) as e:
            # The worker did not answer at all -- that one is gone, not the frame.
            self.pool.replace(worker)
            self._respond(HTTPStatus.BAD_GATEWAY, f"worker {worker.index} failed: {e}".encode())

    def _respond(
        self, status: HTTPStatus, body: bytes, *, content_type: str = "text/plain; charset=utf-8"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve_fleet(
    *,
    host: str,
    port: int,
    workers: int,
    headless: bool,
    width: int,
    height: int,
    frame_timeout: float,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="earth-serve-"))
    config = {
        "headless": headless,
        "width": width,
        "height": height,
        "frame_timeout": frame_timeout,
    }
    pool = WorkerPool(root, workers, config)
    try:
        print(f"Starting {workers} worker(s), each on its own copy of the profile...", flush=True)
        pool.start()

        _DispatchHandler.pool = pool
        _DispatchHandler.frame_timeout_s = frame_timeout
        with ThreadingHTTPServer((host, port), _DispatchHandler) as server:
            print(
                f"Listening on http://{host}:{port} -- "
                "GET /frame?lat=..&lon=..&alt=..&heading=..&tilt=.. "
                f"({width}x{height}, {pool.ready} worker(s))",
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass  # the signal handler already said so
    finally:
        pool.close()
        shutil.rmtree(root, ignore_errors=True)
        print("Server stopped.", flush=True)


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
                "login profile. Throughput scales to about 4x at 8 workers on one GPU, "
                "with per-frame latency roughly doubling -- see serve.py's docstring."
            ),
        ),
    ] = 1,
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
    worker: Annotated[
        bool,
        typer.Option(
            "--worker",
            hidden=True,
            help="Internal: run as one worker of a fleet, on the profile in EARTH_PROFILE_DIR.",
        ),
    ] = False,
) -> None:
    """Serve single Earth Studio frames over HTTP from a pool of warm browsers."""
    if worker:
        _serve_worker(
            host=host,
            port=port,
            headless=headless,
            width=width,
            height=height,
            frame_timeout=frame_timeout,
        )
        return

    _serve_fleet(
        host=host,
        port=port,
        workers=workers,
        headless=headless,
        width=width,
        height=height,
        frame_timeout=frame_timeout,
    )
