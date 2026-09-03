#!/bin/sh
# One-time image setup, run via onCreateCommand.
set -e

# The virtual display / VNC / window-manager / D-Bus + portal stack that
# start-vnc.sh brings up on every container start; apt pulls in the shared
# libraries these and Chromium need.
apt-get update
apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc fluxbox xdotool dbus-x11 xdg-desktop-portal-gtk

# Chromium plus the runtime libs and fonts it needs, per Playwright's own
# per-distro list.
uv run playwright install --with-deps chromium

# The File System Access prompts Earth Studio's local render raises ("Allow
# this site to view and copy files?", "Save changes to <dir>?") are accepted
# from the keyboard by studio_render.start_render() -- Playwright's bundled
# Chrome for Testing ignores the managed-policy file that used to live here,
# so there is nothing to pre-grant.
