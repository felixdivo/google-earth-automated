#!/usr/bin/env bash
# Build the image (cheap when cached) and run a command inside the container
# with the host's NVIDIA GPU passed through for hardware WebGL.
#
#   ./run.sh                                   # interactive shell
#   ./run.sh uv run studio-login               # one-time Google login (see below)
#   ./run.sh uv run esp-builder --lat ... --lon ...
#   ./run.sh uv run studio-render project.esp --headless
#
# The whole repo is bind-mounted at /workspace, so generated .esp files,
# rendered frames under out/, and the login profile under .auth/ all land back
# on the host. noVNC is forwarded on http://localhost:6080/vnc.html for the
# headed studio-login step.
#
# GPU passthrough uses CDI (Docker >= 25). Override which GPUs are visible with
#   GPUS=0 ./run.sh ...            (a single index)
#   GPUS=0,1,2 ./run.sh ...        (a list)
# Default is "all".
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE=google-earth-automated
GPUS="${GPUS:-all}"

mkdir -p "$REPO/.auth" "$REPO/out"

docker build -t "$IMAGE" "$REPO"

exec docker run --rm -it \
    --name google-earth-automated \
    --init \
    --device "nvidia.com/gpu=${GPUS}" \
    --device /dev/fuse \
    --cap-add SYS_ADMIN \
    --publish 6080:6080 \
    --volume "$REPO":/workspace \
    "$IMAGE" "${@:-bash}"
