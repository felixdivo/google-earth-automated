"""Report whether Chromium's WebGL is hardware-accelerated here.

    uv run earth gpu-check

Launches Chromium exactly as `earth render` does and asks a blank page for its
WebGL renderer -- no Earth Studio, no login needed. Use it to check GPU
passthrough before committing to a long render. Exits 0 if a real GPU is in
use, 1 otherwise.
"""

from playwright.sync_api import sync_playwright

from .browser import close_quietly, first_page, gpu_status, launch_context


def command() -> None:
    """Report whether Chromium's WebGL is hardware-accelerated here."""
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=True)
        try:
            page = first_page(context)
            page.goto("about:blank")
            status = gpu_status(page)
        finally:
            close_quietly(context)

    print(status)
    raise SystemExit(0 if status.startswith("GPU: in use") else 1)
