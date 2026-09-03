FROM ghcr.io/bare-devcontainer/uv:trixie

# Everything here runs as root, matching the devcontainer (containerUser: root)
# and the render automation's needs: apt, the Chromium managed policy under
# /etc, the pre-granted --no-sandbox, and the SYS_ADMIN-backed folder picker
# (see the Security section in README.md for the tradeoff this accepts).
USER root

# System stack, three groups:
#  - virtual display / VNC / window-manager / D-Bus + portal: the headed
#    `studio-login` step needs a display, and Earth Studio's native GTK
#    folder-picker needs a WM plus xdg-desktop-portal to appear at all
#    (mirrors .devcontainer/setup.sh + start-vnc.sh).
#  - xdotool: drives that folder picker (it is an OS dialog, not page DOM).
#  - libglvnd / libEGL / libGLES / libGL / libvulkan loaders: Chromium's ANGLE
#    backend dlopen()s these to reach the NVIDIA driver for hardware WebGL.
#    The NVIDIA vendor libraries and their ICD/EGL JSON come from the CDI
#    device mount at runtime; only the loaders belong in the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify fluxbox xdotool dbus-x11 xdg-desktop-portal-gtk \
        libglvnd0 libgl1 libglx0 libegl1 libgles2 libgbm1 libvulkan1 \
    && rm -rf /var/lib/apt/lists/*

# Pre-grant the File System Access API so Earth Studio's local Image Sequence
# render writes frames without a permission bubble (see .devcontainer/setup.sh
# for why a managed policy rather than a screen-coordinate click).
RUN mkdir -p /etc/chromium/policies/managed \
    && printf '%s\n' \
        '{ "DefaultFileSystemReadGuardSetting": 1, "DefaultFileSystemWriteGuardSetting": 1 }' \
        > /etc/chromium/policies/managed/earth-studio.json

WORKDIR /workspace

# Dependency layer: resolves from the lockfile only, so it is cached across
# source edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen \
    && uv run playwright install --with-deps chromium

ENV DISPLAY=:99 \
    XDG_RUNTIME_DIR=/tmp/runtime-root \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-session-bus

ENTRYPOINT ["sh", "/workspace/docker/entrypoint.sh"]
CMD ["bash"]
