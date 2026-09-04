"""Give each worker of an `earth serve` fleet a GPU of its own.

Chromium's Vulkan backend has no flag for picking a GPU: it takes the first
physical device the driver enumerates, so every worker in a fleet piles onto the
same card while the others idle. But NVIDIA's userspace enumerates from the
/dev/nvidiaN nodes, so a worker that cannot see a node does not see that GPU --
and a mount namespace makes "cannot see" a per-process property.

Nothing to configure: `GPUS=14,15 ./run.sh` (or the devcontainer's CDI runArgs)
decides which nodes exist, and one GPU, or an unprivileged container, quietly
means the old single-GPU behaviour.
"""

import os
import subprocess
import time
from pathlib import Path

# How long the probe below gets before it is assumed wedged. Generous: it is a
# fork, two mounts and an exit, measured in milliseconds, and is only ever
# waited out when something is badly wrong.
PIN_PROBE_TIMEOUT_S = 30.0


def gpu_nodes() -> list[Path]:
    """The GPU device nodes this container can see, in a stable order.

    /dev/nvidiaN is one GPU. Which ones exist is decided at the container
    boundary, so this needs no option of its own: whatever was passed in is
    what the fleet spreads over.
    """
    nodes = [p for p in Path("/dev").glob("nvidia*") if p.name[6:].isdigit()]
    return sorted(nodes, key=lambda p: int(p.name[6:]))


def pin_to_gpu(node: Path) -> None:
    """Make the calling process, and everything it starts, see only `node`.

    Called from inside an already-forked worker rather than wrapped around an
    exec: gunicorn forks its workers, so there is no command line to prefix with
    `unshare`. os.unshare() is the same syscall that binary makes, and a mount
    namespace is inherited by every child and every thread created afterwards --
    which is what carries it into Playwright's driver thread and the Chromium
    tree it launches.

    The binds are private, so they are invisible to the rest of the container
    and disappear with the worker.

    Needs CAP_SYS_ADMIN, which `run.sh` and the devcontainer already grant for
    FUSE. `pinning_works` checks before the fleet relies on it.
    """
    hidden = [p for p in gpu_nodes() if p != node]
    if not hidden:
        return
    os.unshare(os.CLONE_NEWNS)
    # A fresh namespace inherits the parent's propagation flags, so without this
    # the binds below would travel back up and hide those GPUs from the whole
    # container rather than from this worker alone.
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    for other in hidden:
        subprocess.run(["mount", "--bind", "/dev/null", str(other)], check=True)


def _pinning_works(node: Path) -> bool:
    """Probe the namespace trick once, so a fleet never half-starts on it.

    Cheap (a few ms) and worth it: unshare or the bind can fail for reasons that
    have nothing to do with this code -- no CAP_SYS_ADMIN, a hostile seccomp
    profile, a host with no `mount` binary -- and a fleet that discovers that N
    times in N workers just fails to start with no explanation.

    Runs the real pin_to_gpu() in a throwaway fork, rather than something that
    merely resembles it, so the probe cannot pass where the workers would fail.
    The fork is safe here because it happens in the CLI process before gunicorn
    (and so any browser or Playwright thread) exists.
    """
    pid = os.fork()
    if pid == 0:
        # os._exit() from a finally, so nothing can escape this branch: an
        # exception that unwound out of here would carry on running the CLI's
        # own code in a second process. A failed pin is the answer, not a crash.
        code = 1
        try:
            pin_to_gpu(node)
            code = 0
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            os._exit(code)

    deadline = time.monotonic() + PIN_PROBE_TIMEOUT_S
    while True:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            return os.waitstatus_to_exitcode(status) == 0
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            return False
        time.sleep(0.01)


def fleet_gpus(workers: int) -> list[Path]:
    """The GPUs worth spreading `workers` over, or [] to leave them unpinned.

    Nothing to gain from pinning a fleet that has one GPU to choose from, or
    fewer workers than GPUs would each get one -- and every case that opts out
    here is the old, unpinned behaviour, unchanged.
    """
    nodes = gpu_nodes()
    if len(nodes) < 2 or workers < 2:
        return []
    if not _pinning_works(nodes[0]):
        print(
            "warning: cannot pin workers to GPUs (needs CAP_SYS_ADMIN) -- "
            f"all {workers} workers will share one of the {len(nodes)} GPUs",
            flush=True,
        )
        return []
    return nodes
