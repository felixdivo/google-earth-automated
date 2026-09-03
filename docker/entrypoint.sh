#!/bin/sh
# Container entrypoint for the `./run.sh` path: bring up the display/VNC/portal
# stack, then hand off to the requested command. (The devcontainer runs
# services.sh itself via postStartCommand and ignores this entrypoint.)
set -e

sh "$(dirname "$0")/services.sh"

# When run.sh bind-mounts the repo over /workspace, the image's baked virtualenv
# is shadowed -- re-materialise it from the lockfile. A no-op once the gitignored
# .venv/ exists on the host.
if [ -f /workspace/pyproject.toml ]; then
    uv sync --frozen
fi

exec "$@"
