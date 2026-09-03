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

            # Verified rather than assumed: Earth Studio authenticates through an
            # App Engine "conflogin" redirect, and a sign-in that stops at the
            # account chooser or a 2-step prompt leaves a profile that looks
            # plausible -- cookies get written either way -- but sends every
            # later `earth render` straight back to the sign-in page. Saying
            # "Session saved" there costs a whole round trip to discover, so
            # reload and confirm Earth Studio actually keeps us.
            page.goto(EARTH_STUDIO_URL)
            page.wait_for_load_state("domcontentloaded")
            logged_in = "studio" in page.url
        finally:
            close_quietly(context)

    if not logged_in:
        raise SystemExit(
            "login did not complete -- Earth Studio still redirects to the Google "
            "sign-in page.\nFinish the sign-in (including any 2-step verification) "
            "and make sure the Earth Studio\nproject browser is on screen before "
            "pressing Enter, then run `earth login` again."
        )
    print("Session saved. You can now run `earth render`.")
