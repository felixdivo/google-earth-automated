"""Shared Playwright plumbing for driving the Google Earth Studio web app.

Uses a persistent browser profile (see PROFILE_DIR) so a login performed once
via studio_login.py is reused by studio_render.py. Headed by default: headless
Chromium needs extra GPU/WebGL flags to render Earth Studio's canvas correctly,
and the first live runs need to be visually debuggable anyway. A headed run
needs a display (X11/VNC forwarded into the container, or run natively on the
host) -- this devcontainer has no display/GPU by default.
"""

from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".auth" / "chrome-profile"

EARTH_STUDIO_URL = "https://earth.google.com/studio"


def launch_context(playwright: Playwright, *, headless: bool = False) -> BrowserContext:
    """Launch (or reattach to) the persistent Earth Studio browser profile."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(PROFILE_DIR, headless=headless)


def first_page(context: BrowserContext) -> Page:
    """Return the context's initial tab, or open one if it has none."""
    return context.pages[0] if context.pages else context.new_page()
