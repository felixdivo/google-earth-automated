FROM ghcr.io/bare-devcontainer/uv:trixie

# Everything here runs as root, matching the devcontainer (containerUser: root)
# and the render automation's needs: apt, the pre-granted --no-sandbox, and the
# SYS_ADMIN-backed folder picker (see the Security section in README.md for the
# tradeoff this accepts).
USER root

# System stack, three groups. docker/services.sh starts the processes these
# provide; both the ./run.sh entrypoint and the devcontainer run that script.
#  - virtual display / VNC / window-manager / D-Bus + portal: the headed
#    `earth login` step needs a display, and Earth Studio's native GTK
#    folder-picker needs a WM plus xdg-desktop-portal to appear at all.
#  - xdotool: drives that folder picker (it is an OS dialog, not page DOM).
#  - libglvnd / libEGL / libGLES / libGL / libvulkan loaders: Chromium's ANGLE
#    backend dlopen()s these to reach the NVIDIA driver for hardware WebGL.
#    The NVIDIA vendor libraries and their ICD/EGL JSON come from the CDI
#    device mount at runtime; only the loaders belong in the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tigervnc-standalone-server novnc websockify fluxbox xdotool dbus-x11 xdg-desktop-portal-gtk \
        libglvnd0 libgl1 libglx0 libegl1 libgles2 libgbm1 libvulkan1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Dependency layer: resolves from the lockfile only, so it is cached across
# source edits. `uv run playwright install` only needs the dependencies (it
# invokes the `playwright` package, not this project's own code -- `--no-sync`
# stops `uv run` from trying to build+install the project too, which would
# fail here since the source isn't copied in yet), so it runs here too -- that
# keeps the slow browser download cached across source edits as well, instead
# of re-fetching on every change under COPY . . below. `--with-deps` pulls its
# own apt packages (fonts, X11/GTK runtime libs) via `apt-get`, which leaves a
# second populated /var/lib/apt/lists behind.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project \
    && uv run --no-sync playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Installs this project's own code into the venv (the previous sync explicitly
# skipped it), so the image is a complete, runnable environment on its own.
# `./run.sh` bind-mounts the repo over /workspace at container start, which
# shadows this venv with the host's (gitignored, .venv-less) copy --
# docker/entrypoint.sh re-syncs at that point to restore it; this one is what
# makes the image work when run without that bind mount.
COPY . .
RUN uv sync --frozen

ENV DISPLAY=:99 \
    XDG_RUNTIME_DIR=/tmp/runtime-root \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-session-bus

ENTRYPOINT ["sh", "/workspace/docker/entrypoint.sh"]
CMD ["bash"]
