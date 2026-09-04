"""Make an interrupt a clean exit rather than a traceback plus driver noise.

Every command here drives Chromium through Playwright's sync API, which runs
its event loop on a second thread. An interrupt lands in the main thread
mid-call, so the loop is left holding a task that will never be awaited
(`Page.goto()`, most often), and tearing it down then logs, on top of whatever
traceback Python prints:

    Task was destroyed but it is pending!
    Future exception was never retrieved
    playwright._impl._errors.TargetClosedError: Target page, context or browser has been closed

None of that is actionable -- it is the browser being closed while a call was
in flight, which is exactly what Ctrl-C asked for. The handler below silences
it at the source, then raises KeyboardInterrupt so the interrupt propagates
normally and each command's own `finally` gets to close its browser. SIGTERM
is handled the same way, so `docker stop` on a long-running `earth serve` shuts
down as gracefully as Ctrl-C does.

Cleanup gets GRACE_PERIOD_S and no longer, because it can hang outright:
interrupting a browser *launch* leaves Playwright's teardown waiting forever on
a call that will never come back. A second signal ends it immediately too, so
nobody has to sit through the grace period. Either way the exit takes the
browser with it: Playwright's driver cleans up after a client that disappears
while it is *idle*, but one interrupted mid-operation leaves the node driver
and the whole Chromium tree running indefinitely (measured -- they were still
alive minutes later), and on a render host that is a leaked GPU.
"""

import logging
import os
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import suppress
from types import FrameType

# Long enough for a real context.close() on a busy browser, short enough that a
# stuck shutdown does not feel like a freeze.
GRACE_PERIOD_S = 6.0

_exit_code = 130  # 128 + SIGINT, replaced by whichever signal actually arrives
_interrupted = False


def _descendants(pid: int) -> Iterator[int]:
    """Every process below pid, children before parents.

    Not os.killpg(): that needs os.setsid(), and a session leader no longer
    receives Ctrl-C from the terminal's foreground group -- which is the one
    thing this module exists to handle. It would also miss any descendant
    that starts a session of its own, silently restoring the leak this walk
    was written to fix.
    """
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            children = f.read().split()
    except OSError:
        return  # already gone, or not Linux -- nothing to kill either way
    for child in map(int, children):
        yield from _descendants(child)
        yield child


def _force_exit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    sys.stdout.flush()
    # Only our own descendants: the Playwright driver and the Chromium tree it
    # spawned, never anything else that happens to be running on this host.
    for pid in _descendants(os.getpid()):
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    os._exit(_exit_code)


def _on_signal(signum: int, _frame: FrameType | None) -> None:
    global _exit_code, _interrupted
    if _interrupted:
        _force_exit("Still shutting down -- forcing exit.")
    _interrupted = True
    _exit_code = 128 + signum

    # Said now rather than after cleanup: closing a browser takes a moment, and
    # silence in that moment is what makes people hit Ctrl-C again.
    name = "Interrupted" if signum == signal.SIGINT else "Terminated"
    print(f"\n{name} -- shutting down (again to force).", file=sys.stderr, flush=True)

    # Playwright's loop thread is about to be torn down with calls still in
    # flight; its complaints about that are shutdown noise (see module docstring).
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)

    # A thread, not signal.alarm(): the hang this guards against blocks the main
    # thread, which is where signal handlers would have to run. Daemon, so a
    # shutdown that finishes in time is not held up by it.
    watchdog = threading.Timer(
        GRACE_PERIOD_S,
        _force_exit,
        args=(f"Shutdown stalled for {GRACE_PERIOD_S:.0f}s -- forcing exit.",),
    )
    watchdog.daemon = True
    watchdog.start()

    raise KeyboardInterrupt


def install_signal_handlers() -> None:
    """Route SIGINT/SIGTERM into the graceful shutdown path, for good."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _on_signal)
