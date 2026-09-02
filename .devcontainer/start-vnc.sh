#!/bin/sh
# Starts a virtual display + VNC/noVNC so headed Chromium (needed for the
# Earth Studio login step, `uv run studio-login`) can be viewed in a normal
# browser tab via the forwarded 6080 port. Also starts a window manager and
# the D-Bus/portal stack: Earth Studio's local image-sequence render asks for
# an output folder via the File System Access API, which Chromium serves via
# a native GTK folder-picker dialog -- that dialog needs a WM to be shown at
# all, and a D-Bus session + xdg-desktop-portal(-gtk) to exist in the first
# place. Idempotent -- safe to run on every container start (postStartCommand).
#
# Each process is launched via `setsid` so it becomes its own session leader,
# detached from this script's process group -- devcontainer's postStartCommand
# tears down the invoking shell right after it returns, which otherwise sends
# SIGHUP to plain `&` background jobs. Xvfb ignores SIGHUP so it looked fine,
# but the rest would die (or never even start) without this.
set -e

if ! pgrep -f "Xvfb :99" >/dev/null 2>&1; then
    setsid Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 < /dev/null &
fi
sleep 1

if ! pgrep -f "x11vnc -display :99" >/dev/null 2>&1; then
    setsid x11vnc -display :99 -forever -shared -nopw -quiet >/tmp/x11vnc.log 2>&1 < /dev/null &
fi
sleep 1

if ! pgrep -f "websockify.*6080" >/dev/null 2>&1; then
    setsid websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/tmp/websockify.log 2>&1 < /dev/null &
fi

if ! pgrep -x fluxbox >/dev/null 2>&1; then
    DISPLAY=:99 setsid fluxbox >/tmp/fluxbox.log 2>&1 < /dev/null &
    sleep 1
    # fluxbox's default init tries (and, on a blank container, fails) to set a
    # wallpaper, which pops up a blocking "something went wrong" dialog no one
    # is there to dismiss.
    pkill -f "xmessage.*fbsetbg" 2>/dev/null || true
fi

mkdir -p /tmp/runtime-root
chmod 700 /tmp/runtime-root

if ! pgrep -f "dbus-daemon --session" >/dev/null 2>&1; then
    rm -f /tmp/dbus-session-bus
    setsid dbus-daemon --session --address=unix:path=/tmp/dbus-session-bus --nofork --nopidfile >/tmp/dbus.log 2>&1 < /dev/null &
fi
sleep 1

if ! pgrep -f xdg-desktop-portal-gtk >/dev/null 2>&1; then
    XDG_RUNTIME_DIR=/tmp/runtime-root DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-session-bus \
        setsid /usr/libexec/xdg-desktop-portal-gtk >/tmp/portal-gtk.log 2>&1 < /dev/null &
fi

if ! pgrep -f "/usr/libexec/xdg-desktop-portal$" >/dev/null 2>&1; then
    XDG_RUNTIME_DIR=/tmp/runtime-root DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/dbus-session-bus \
        setsid /usr/libexec/xdg-desktop-portal >/tmp/portal.log 2>&1 < /dev/null &
fi
