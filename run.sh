#!/usr/bin/env bash
# Build the image (cheap when cached) and run a command inside the container
# with the host's NVIDIA GPU passed through for hardware WebGL. The devcontainer
# builds the same Dockerfile and passes the same runtime flags.
#
#   ./run.sh                                   # interactive shell
#   ./run.sh uv run earth login                # one-time Google login (see below)
#   ./run.sh uv run earth build --lat ... --lon ...
#   ./run.sh uv run earth render project.esp --headless
#   ./run.sh uv run earth serve                # single-frame render API on :8000
#
# The whole repo is bind-mounted at /workspace, so generated .esp files,
# rendered frames under out/, and the login profile under .auth/ all land back
# on the host. noVNC is forwarded on http://localhost:6080/vnc.html for the
# headed `earth login` step, and `earth serve`'s API on http://localhost:8000.
#
# GPU passthrough uses CDI (Docker >= 25). GPUS is a comma-separated list of
# device indices from `nvidia-ctk cdi list`, or "all":
#   GPUS=all ./run.sh ...          # every GPU
#   GPUS=0 ./run.sh ...            # just GPU 0
# Default is the last two GPUs on this 16-GPU host, leaving the rest free.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE=google-earth-automated
GPUS="${GPUS:-14,15}"

gpu_args=()
if [ "$GPUS" = "all" ]; then
    gpu_args=(--device "nvidia.com/gpu=all")
else
    IFS=',' read -ra _gpus <<<"$GPUS"
    for g in "${_gpus[@]}"; do
        gpu_args+=(--device "nvidia.com/gpu=${g}")
    done
fi

mkdir -p "$REPO/.auth" "$REPO/out"

docker build -t "$IMAGE" "$REPO"

exec docker run --rm -it \
    --name google-earth-automated \
    --init \
    "${gpu_args[@]}" \
    --device /dev/fuse \
    --cap-add SYS_ADMIN \
    --publish 6080:6080 \
    --publish 8000:8000 \
    --volume "$REPO":/workspace \
    "$IMAGE" "${@:-bash}"
