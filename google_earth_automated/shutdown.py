"""Make an interrupt a clean exit rather than a traceback plus driver noise.

Every command here drives Chromium through Playwright's sync API, which runs
its asyncio event loop on a second thread. A signal lands in the main thread
mid-call, so the loop is left holding a task that will never be awaited
(`Page.goto()`, most often) -- and tearing it down then logs, on top of
whatever traceback Python prints:

    Task was destroyed but it is pending!
    Future exception was never retrieved
    playwright._impl._errors.TargetClosedError: Target page, context or browser has been closed

None of that is actionable: it is the browser being closed while a call was in
flight, which is exactly what Ctrl-C asked for. install_signal_handlers()
silences it at the source -- the moment the first signal arrives, before any
teardown runs -- and then raises KeyboardInterrupt so the interrupt propagates
normally, letting each command's own `finally` blocks close the browser.

SIGTERM is handled the same way, so `docker stop` on a long-running
`earth serve` shuts the session down as gracefully as Ctrl-C does.

Cleanup is given GRACE_PERIOD_S to finish and no longer. It can hang outright:
interrupting a browser *launch* leaves Playwright's teardown waiting forever on
a call that will never come back, which without the watchdog below needs a
SIGKILL from another terminal. A second signal -- one arriving while cleanup is
still running -- ends it immediately too, so the user never has to wait out the
grace period.
"""

import logging
import os
import signal
import sys
import threading
from types import FrameType

_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)

# How long the commands' own cleanup (closing the browser, clearing scratch
# files) gets before it is taken as hung. Generous enough for a real
# context.close() on a busy browser, short enough not to feel like a freeze.
GRACE_PERIOD_S = 6.0

# By convention a process killed by signal N reports 128 + N. SIGINT until a
# signal actually arrives, so an interrupt raised from anywhere else (a
# KeyboardInterrupt out of input(), say) still exits as one.
exit_code = 128 + int(signal.SIGINT)

_interrupted = False


def _kill_browser_processes() -> None:
    """SIGKILL every descendant -- the Playwright driver and the Chromium tree
    it launched. Only used when shutting down gracefully has already failed:
    hard-exiting without this can orphan a whole browser (and, on a render
    host, its share of the GPU).
    """
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            # /proc/<pid>/stat is "<pid> (<comm>) <state> <ppid> ..."; comm can
            # itself contain spaces and parentheses, so split after the last ')'.
            with open(f"/proc/{entry}/stat") as f:
                fields = f.read().rpartition(")")[2].split()
            ppid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))

    queue = [os.getpid()]
    doomed = []
    while queue:  # breadth-first, so parents cannot fork out of reach
        for pid in children.get(queue.pop(), ()):
            doomed.append(pid)
            queue.append(pid)
    for pid in doomed:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _force_exit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    sys.stdout.flush()
    _kill_browser_processes()
    os._exit(exit_code)


def _on_signal(signum: int, _frame: FrameType | None) -> None:
    global _interrupted, exit_code
    if _interrupted:
        _force_exit("Still shutting down -- forcing exit.")
    _interrupted = True
    exit_code = 128 + signum
    # Said now rather than after cleanup: shutting a browser down takes a
    # moment, and silence in that moment is what makes people hit Ctrl-C again.
    name = "Interrupted" if signum == signal.SIGINT else "Terminated"
    print(f"\n{name} -- shutting down (again to force).", file=sys.stderr, flush=True)
    # Playwright's loop thread is about to be torn down with calls still in
    # flight; its complaints about that are shutdown artifacts (see module
    # docstring), and the process is on its way out anyway.
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    # Daemon, so a shutdown that finishes in time is not held up by it.
    watchdog = threading.Timer(
        GRACE_PERIOD_S,
        _force_exit,
        args=(f"Shutdown did not finish within {GRACE_PERIOD_S:.0f}s -- forcing exit.",),
    )
    watchdog.daemon = True
    watchdog.start()
    raise KeyboardInterrupt


def install_signal_handlers() -> None:
    """Route SIGINT/SIGTERM into the graceful shutdown path, for good."""
    for sig in _HANDLED_SIGNALS:
        signal.signal(sig, _on_signal)
