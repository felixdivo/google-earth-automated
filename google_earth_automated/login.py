"""One-time interactive login for Earth Studio automation.

Run this once (and again whenever the session expires):

    uv run earth login

It opens a real, visible browser window pointed at Earth Studio, using the
persistent profile in .auth/chrome-profile/. Log in with your Google account
in that window, then come back to the terminal and press Enter -- the session
is saved to disk and reused by `earth render`.
"""

from playwright.sync_api import sync_playwright

from .browser import EARTH_STUDIO_URL, close_quietly, first_page, launch_context


def command() -> None:
    """Open a browser to log in to Google Earth Studio and save the session."""
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=False)
        # The wait below is where this command spends nearly all its life, so
        # it is where a Ctrl-C (or a Ctrl-D out of input()) almost always
        # lands -- close the browser window on the way out either way.
        try:
            page = first_page(context)
            page.goto(EARTH_STUDIO_URL)

            input(
                "A browser window has opened. Log in with your Google account, wait "
                "until you can see the Earth Studio project browser, then press "
                "Enter here...\n"
            )
        finally:
            close_quietly(context)

    print("Session saved. You can now run `earth render`.")
