"""Render an Earth Studio .esp project to a PNG image sequence.

Drives the real earth.google.com/studio web app with Playwright: imports the
given .esp file, sets the render format to an Image Sequence (PNG), triggers
the render, waits for Earth Studio's in-browser render to finish and download
a zip of frames, then extracts that zip into --output-dir. Encoding the
sequence into a video is a separate, later step -- not done here.

Requires a logged-in session -- run studio_login.py first.

    uv run src/studio_render.py frankfurt_hauptbahnhof_orbit.esp
    uv run src/studio_render.py frankfurt_hauptbahnhof_orbit.esp --output-dir out/ --headless

Earth Studio's UI is an undocumented, JS-heavy SPA, so the selectors below are
best-effort and may need adjusting after the first live run -- this is the one
place to fix them.
"""

import argparse
import json
import sys
import zipfile
from pathlib import Path

from earth_studio_browser import EARTH_STUDIO_URL, first_page, launch_context
from playwright.sync_api import sync_playwright

# --- Earth Studio UI selectors (best-effort; fix here if a step fails) -----
IMPORT_MENU_ITEM = "text=Import"
RENDER_BUTTON = "text=Render"
FORMAT_IMAGE_SEQUENCE_OPTION = "text=Image Sequence"
START_RENDER_BUTTON = "button:has-text('Render')"
# -----------------------------------------------------------------------------

RENDER_TIMEOUT_MS = 30 * 60 * 1000  # in-browser rendering can take a while


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an Earth Studio .esp project to a PNG image sequence.")
    parser.add_argument("esp_file", type=Path, help="Path to the .esp project file to render")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to extract the rendered PNG sequence into (default: <esp-stem>_frames/)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser headless (default: headed, recommended until selectors are verified)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate arguments and the .esp file, print the planned actions, and exit without launching a browser",
    )
    args = parser.parse_args(argv)
    if args.output_dir is None:
        args.output_dir = Path(f"{args.esp_file.stem}_frames")
    return args


def validate_esp_file(esp_file: Path) -> None:
    if not esp_file.is_file():
        sys.exit(f"error: {esp_file} does not exist")
    try:
        json.loads(esp_file.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"error: {esp_file} is not valid JSON: {e}")


def render(esp_file: Path, output_dir: Path, *, headless: bool) -> None:
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=headless)
        page = first_page(context)
        page.goto(EARTH_STUDIO_URL)

        if "studio" not in page.url:
            context.close()
            sys.exit("error: not logged in -- run `uv run src/studio_login.py` first")

        with page.expect_file_chooser() as file_chooser_info:
            page.click(IMPORT_MENU_ITEM)
        file_chooser_info.value.set_files(str(esp_file))

        page.click(RENDER_BUTTON)
        page.click(FORMAT_IMAGE_SEQUENCE_OPTION)

        with page.expect_download(timeout=RENDER_TIMEOUT_MS) as download_info:
            page.click(START_RENDER_BUTTON)

        output_dir.mkdir(parents=True, exist_ok=True)
        zip_path = output_dir / "render.zip"
        download_info.value.save_as(zip_path)

        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(output_dir)
        zip_path.unlink()

        context.close()
        print(f"Rendered frames extracted to {output_dir}/")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    validate_esp_file(args.esp_file)

    if args.dry_run:
        print(f"Would import {args.esp_file} into Earth Studio")
        print("Would set render format to Image Sequence (PNG)")
        print(f"Would extract the rendered frames to {args.output_dir}/")
        return

    render(args.esp_file, args.output_dir, headless=args.headless)


if __name__ == "__main__":
    main()
