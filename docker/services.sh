#!/bin/sh
# Bring up the virtual display / VNC / window-manager / D-Bus + portal stack the
# render automation needs:
#  - headed Chromium (the `earth login` step) needs a display; noVNC on 6080
#    makes it viewable in a normal browser tab.
#  - Earth Studio's local render opens a native GTK folder picker, which needs a
#    window manager to be shown at all plus a D-Bus session + xdg-desktop-portal
#    to exist in the first place.
#
# Idempotent -- safe to run repeatedly. Used by both docker/entrypoint.sh
# (the `./run.sh` path) and the devcontainer's postStartCommand.
#
# Each process is launched via `setsid ... </dev/null &` so it survives the
# caller's shell being torn down -- the devcontainer's postStartCommand does
# exactly that right after this returns, which would otherwise SIGHUP the jobs.
# Harmless when called from the long-lived entrypoint.
set -e

if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
    setsid Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 </dev/null &
fi
sleep 1

if ! pgrep -f "x11vnc -display :99" >/dev/null 2>&1; then
    setsid x11vnc -display :99 -forever -shared -nopw -quiet >/tmp/x11vnc.log 2>&1 </dev/null &
fi
sleep 1

if ! pgrep -f "websockify.*6080" >/dev/null 2>&1; then
    setsid websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/tmp/websockify.log 2>&1 </dev/null &
fi

if ! pgrep -x fluxbox >/dev/null 2>&1; then
    DISPLAY=:99 setsid fluxbox >/tmp/fluxbox.log 2>&1 </dev/null &
    sleep 1
    # fluxbox's default init fails to set a wallpaper on a blank container and
    # pops a blocking dialog no one is there to dismiss.
    pkill -f "xmessage.*fbsetbg" 2>/dev/null || true
fi

mkdir -p /tmp/runtime-root
chmod 700 /tmp/runtime-root

if ! pgrep -f "dbus-daemon --session" >/dev/null 2>&1; then
    rm -f /tmp/dbus-session-bus
    setsid dbus-daemon --session --address=unix:path=/tmp/dbus-session-bus --nofork --nopidfile \
        >/tmp/dbus.log 2>&1 </dev/null &
fi
sleep 1

if ! pgrep -f xdg-desktop-portal-gtk >/dev/null 2>&1; then
    XDG_RUNTIME_DIR=/tmp/runtime-root DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-session-bus \
        setsid /usr/libexec/xdg-desktop-portal-gtk >/tmp/portal-gtk.log 2>&1 </dev/null &
fi

if ! pgrep -f "/usr/libexec/xdg-desktop-portal$" >/dev/null 2>&1; then
    XDG_RUNTIME_DIR=/tmp/runtime-root DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-session-bus \
        setsid /usr/libexec/xdg-desktop-portal >/tmp/portal.log 2>&1 </dev/null &
fi
