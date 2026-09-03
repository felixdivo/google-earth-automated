"""Report whether Chromium's WebGL is hardware-accelerated here.

    uv run studio-gpu-check

Launches Chromium exactly as studio-render does and asks a blank page for its
WebGL renderer -- no Earth Studio, no login needed. Use it to check GPU
passthrough before committing to a long render. Exits 0 if a real GPU is in
use, 1 otherwise.
"""

import argparse

from playwright.sync_api import sync_playwright

from .earth_studio_browser import first_page, gpu_status, launch_context


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)

    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=True)
        page = first_page(context)
        page.goto("about:blank")
        status = gpu_status(page)
        context.close()

    print(status)
    raise SystemExit(0 if status.startswith("GPU: in use") else 1)


if __name__ == "__main__":
    main()
