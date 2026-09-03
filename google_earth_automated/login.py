"""One-time interactive login for Earth Studio automation.

Run this once (and again whenever the session expires):

    uv run earth login

It opens a real, visible browser window pointed at Earth Studio, using the
persistent profile in .auth/chrome-profile/. Log in with your Google account
in that window, then come back to the terminal and press Enter -- the session
is saved to disk and reused by `earth render`.
"""

from playwright.sync_api import sync_playwright

from .browser import EARTH_STUDIO_URL, first_page, launch_context


def command() -> None:
    """Open a browser to log in to Google Earth Studio and save the session."""
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=False)
        page = first_page(context)
        page.goto(EARTH_STUDIO_URL)

        input(
            "A browser window has opened. Log in with your Google account, wait "
            "until you can see the Earth Studio project browser, then press "
            "Enter here...\n"
        )

        context.close()
        print("Session saved. You can now run `earth render`.")
