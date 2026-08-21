#!/bin/sh
# One-time image setup, run via onCreateCommand. Installs Chromium's runtime
# shared libs (so --headless works out of the box) plus the virtual
# display/VNC/window-manager/D-Bus/portal stack that start-vnc.sh brings up on
# every container start, then installs Playwright's bundled Chromium build.
set -e

apt-get update
apt-get install -y --no-install-recommends \
    libglib2.0-0t64 libnspr4 libnss3 libatk1.0-0t64 libatk-bridge2.0-0t64 \
    libdbus-1-3 libcups2t64 libxcb1 libxkbcommon0 libasound2t64 libgbm1 \
    libx11-6 libxext6 libcairo2 libpango-1.0-0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libatspi2.0-0t64 libgtk-3-0t64 \
    xvfb x11vnc novnc websockify fluxbox xdotool dbus dbus-x11 \
    xdg-desktop-portal xdg-desktop-portal-gtk

uv run playwright install chromium
