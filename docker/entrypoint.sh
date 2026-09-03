#!/bin/sh
# Container entrypoint: bring up the virtual display / VNC / WM / D-Bus + portal
# stack (same processes as .devcontainer/start-vnc.sh, which the devcontainer
# runs on every start), then hand off to the requested command.
set -e

if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
    Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &
fi
sleep 1

if ! pgrep -f "x11vnc -display :99" >/dev/null 2>&1; then
    x11vnc -display :99 -forever -shared -nopw -quiet >/tmp/x11vnc.log 2>&1 &
fi
sleep 1

if ! pgrep -f "websockify.*6080" >/dev/null 2>&1; then
    websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/tmp/websockify.log 2>&1 &
fi

if ! pgrep -x fluxbox >/dev/null 2>&1; then
    DISPLAY=:99 fluxbox >/tmp/fluxbox.log 2>&1 &
    sleep 1
    # fluxbox's default init fails to set a wallpaper on a blank container and
    # pops a blocking dialog no one is there to dismiss.
    pkill -f "xmessage.*fbsetbg" 2>/dev/null || true
fi

mkdir -p /tmp/runtime-root
chmod 700 /tmp/runtime-root

if ! pgrep -f "dbus-daemon --session" >/dev/null 2>&1; then
    rm -f /tmp/dbus-session-bus
    dbus-daemon --session --address=unix:path=/tmp/dbus-session-bus --nofork --nopidfile \
        >/tmp/dbus.log 2>&1 &
fi
sleep 1

if ! pgrep -f xdg-desktop-portal-gtk >/dev/null 2>&1; then
    /usr/libexec/xdg-desktop-portal-gtk >/tmp/portal-gtk.log 2>&1 &
fi

if ! pgrep -f "/usr/libexec/xdg-desktop-portal$" >/dev/null 2>&1; then
    /usr/libexec/xdg-desktop-portal >/tmp/portal.log 2>&1 &
fi

# When the repo is bind-mounted over /workspace (run.sh does this so code edits
# and generated .esp files are picked up without a rebuild), the image's baked
# virtualenv is shadowed -- re-materialise it from the lockfile. A no-op once
# the gitignored .venv/ exists on the host.
if [ -f /workspace/pyproject.toml ]; then
    uv sync --frozen
fi

exec "$@"
