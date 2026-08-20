#!/bin/sh
# Starts a virtual display + VNC/noVNC so headed Chromium (needed for the
# Earth Studio login step in src/studio_login.py) can be viewed in a normal
# browser tab via the forwarded 6080 port. Idempotent -- safe to run on every
# container start (postStartCommand).
#
# Each process is launched via `setsid` so it becomes its own session leader,
# detached from this script's process group -- devcontainer's postStartCommand
# tears down the invoking shell right after it returns, which otherwise sends
# SIGHUP to plain `&` background jobs. Xvfb ignores SIGHUP so it looked fine,
# but x11vnc and websockify would die (or never even start) without this.
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
