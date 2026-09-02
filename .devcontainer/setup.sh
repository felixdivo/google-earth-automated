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

# Pre-grant the File System Access API so Earth Studio's local Image Sequence
# render writes frames without a permission bubble. Those bubbles are drawn
# inside the browser window rather than as DOM or separate X windows, so the
# only way to accept them is a screen-coordinate click -- which silently
# breaks whenever the window geometry or UI font metrics shift. A managed
# policy removes the prompt entirely; 1 means "allow", 3 would be "ask".
mkdir -p /etc/chromium/policies/managed
cat > /etc/chromium/policies/managed/earth-studio.json <<'JSON'
{
  "DefaultFileSystemReadGuardSetting": 1,
  "DefaultFileSystemWriteGuardSetting": 1
}
JSON
