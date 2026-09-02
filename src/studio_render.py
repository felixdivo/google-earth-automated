"""Render an Earth Studio .esp project to a JPEG image sequence.

Drives the real earth.google.com/studio web app with Playwright: imports the
given .esp file, opens the Render panel (only local output format is an Image
Sequence of JPEGs -- there is no PNG or Cloud-free option), grants it
--output-dir as the destination via the native OS folder picker, triggers the
render, and waits for frames to show up in that directory. Encoding the
sequence into a video is a separate, later step -- not done here.

Requires a logged-in session -- run studio_login.py first.

    uv run src/studio_render.py frankfurt_hauptbahnhof_orbit.esp
    uv run src/studio_render.py frankfurt_hauptbahnhof_orbit.esp --output-dir out/ --headless

Local Image Sequence rendering grants filesystem access via the browser's
File System Access API (window.showDirectoryPicker()), which Chromium serves
as a *native OS dialog* -- outside Playwright's DOM/CDP control entirely.
select_output_folder() below drives that dialog with xdotool instead, and
depends on the devcontainer's D-Bus/portal/window-manager stack
(.devcontainer/start-vnc.sh) plus /dev/fuse + CAP_SYS_ADMIN (see
devcontainer.json and the Security section in README.md) -- without those,
the dialog opens but its confirmation hangs forever.

Earth Studio's UI is an undocumented, JS-heavy SPA, so the selectors below are
best-effort and may need adjusting after the first live run -- this is the one
place to fix them.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from earth_studio_browser import (
    EARTH_STUDIO_URL,
    first_page,
    gpu_status,
    launch_context,
)

# --- Earth Studio UI selectors (best-effort; fix here if a step fails) -----
IMPORT_TOGGLE = "div.split-button:has(button[data-action='click:openProject']) button.toggle-dropdown"
IMPORT_MENU_ITEM = "div[data-action='click:importProject']"
RENDER_BUTTON = "text=Render"
FORMAT_IMAGE_SEQUENCE_OPTION = "text=Image sequence (.jpeg)"
CHOOSE_FOLDER_BUTTON = "#output-dir-btn"  # reads "Choose folder" only the first time; a remembered destination replaces the label with its name
START_RENDER_BUTTON = "button[data-action='click:onStartClick']"
# -----------------------------------------------------------------------------

RENDER_TIMEOUT_MS = 3 * 60 * 60 * 1000  # SwiftShader software rendering (no real GPU here) is slow -- give it room

PROJECT_LOAD_WAIT_MS = 8000  # importing a project re-renders the whole scene


def _xdotool(*args: str) -> str:
    result = subprocess.run(["xdotool", *args], capture_output=True, text=True, check=False)
    return result.stdout.strip()


DIALOG_TITLE_PATTERN = "Select a folder"


def _find_window(name_pattern: str, *, timeout_s: float) -> str | None:
    deadline = time.monotonic() + timeout_s
    wid = None
    while time.monotonic() < deadline:
        matches = _xdotool("search", "--name", name_pattern).splitlines()
        if matches:
            wid = matches[-1]  # most recently mapped match
            break
        time.sleep(0.3)
    return wid


def select_output_folder(page: Page, output_dir: Path, *, dialog_timeout_s: float = 10.0) -> None:
    """Drive the native GTK folder picker Earth Studio opens for the render
    destination. This is an OS-level dialog, not a page element, so it's
    automated with xdotool rather than Playwright.

    Matched by window *title*, not by diffing the window list before/after --
    opening the dialog also spawns extra untitled xdg-desktop-portal-gtk
    helper windows, and diffing isn't guaranteed to pick the actual dialog
    over one of those (sending input to the wrong one looks identical to the
    dialog just silently not responding).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    page.click(CHOOSE_FOLDER_BUTTON)

    wid = _find_window(DIALOG_TITLE_PATTERN, timeout_s=dialog_timeout_s)
    if wid is None:
        raise RuntimeError("folder picker dialog never appeared -- check the D-Bus/portal/WM stack is running")

    _xdotool("windowactivate", "--sync", wid)
    time.sleep(0.5)
    _xdotool("key", "--window", wid, "ctrl+l")
    time.sleep(0.3)
    _xdotool("type", "--window", wid, str(output_dir))
    time.sleep(0.3)
    _xdotool("key", "--window", wid, "Return")
    time.sleep(1.0)

    # Navigating via the location-bar entry usually recreates the dialog as a
    # new X window (the old wid becomes a BadWindow) -- re-resolve it by
    # title. But if the typed path was unambiguous, GTK sometimes confirms
    # the pick outright instead and no window reappears at all -- that's
    # success too, not a failure.
    wid = _find_window(DIALOG_TITLE_PATTERN, timeout_s=3.0)
    if wid is None:
        return

    geometry = dict(line.split("=", 1) for line in _xdotool("getwindowgeometry", "--shell", wid).splitlines() if "=" in line)
    x, y, width, height = (int(geometry[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))
    # The "Select Folder" button sits in the dialog's bottom-right action area.
    # There's no stable selector for a native window, so this is a fixed
    # offset from the corner, measured empirically against this dialog's
    # layout -- fragile if that layout ever changes.
    _xdotool("mousemove", "--sync", str(x + width - 95), str(y + height - 35))
    time.sleep(0.2)
    _xdotool("click", "1")

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if wid not in _xdotool("search", "--name", DIALOG_TITLE_PATTERN).splitlines():
            return
        time.sleep(0.5)
    raise RuntimeError(
        "folder picker did not close after confirming the output folder -- "
        "check /dev/fuse + CAP_SYS_ADMIN are granted (see devcontainer.json / README Security section)"
    )


# Primary (blue) button position for Chromium's file-system permission
# bubbles ("Allow this site to view and copy files?", "Save changes to
# <folder>?"). These are browser-chrome UI, not DOM elements or separate
# windows, so they can't be clicked via Playwright or matched by title like
# the folder picker -- this is a fixed absolute screen position instead,
# which holds because this devcontainer always runs a single, borderless
# Chromium window filling the fixed 1280x800 virtual display. There's also no
# enterprise policy to silently pre-approve either prompt (only Ask/Block),
# so clicking through them is unavoidable.
PERMISSION_BUBBLE_BUTTON = (784, 143)


def click_permission_bubble() -> None:
    time.sleep(1.0)  # give the bubble time to render
    x, y = PERMISSION_BUBBLE_BUTTON
    _xdotool("mousemove", "--sync", str(x), str(y))
    time.sleep(0.2)
    _xdotool("click", "1")


def wait_for_frames(
    output_dir: Path,
    *,
    expected_frame_count: int | None,
    timeout_ms: int,
    quiet_period_s: float = 60.0,
    poll_interval_s: float = 2.0,
) -> None:
    """Local Image Sequence rendering writes frames directly into output_dir
    (in a project-named subfolder) via the File System Access API -- there's
    no browser download event to wait on. Primarily considered done once the
    number of JPEG files reaches expected_frame_count (read from the .esp's
    settings.duration); if that count isn't available, falls back to no new
    JPEGs appearing for quiet_period_s. That fallback's window is long
    because SwiftShader software rendering (this devcontainer has no real
    GPU) can take well over a minute per frame -- a short quiet period would
    mistake "still rendering the next frame" for "done".
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last_count = -1
    stable_since = None
    while time.monotonic() < deadline:
        count = len(list(output_dir.rglob("*.jpg"))) + len(list(output_dir.rglob("*.jpeg")))
        if expected_frame_count is not None and count >= expected_frame_count:
            return
        if count != last_count:
            last_count = count
            stable_since = time.monotonic()
        elif count > 0 and stable_since is not None and time.monotonic() - stable_since >= quiet_period_s:
            return
        time.sleep(poll_interval_s)
    raise TimeoutError(f"no stable output detected in {output_dir} within the timeout")


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "out"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an Earth Studio .esp project to a JPEG image sequence.")
    parser.add_argument("esp_file", type=Path, help="Path to the .esp project file to render")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Directory to render the JPEG sequence into (default: out/ at the repo root). "
            "Earth Studio nests frames under a project-name subfolder of this directory on its own."
        ),
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
    return parser.parse_args(argv)


def validate_esp_file(esp_file: Path) -> dict:
    if not esp_file.is_file():
        sys.exit(f"error: {esp_file} does not exist")
    try:
        return json.loads(esp_file.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"error: {esp_file} is not valid JSON: {e}")


def render(esp_file: Path, output_dir: Path, *, headless: bool, expected_frame_count: int | None) -> None:
    with sync_playwright() as playwright:
        context = launch_context(playwright, headless=headless)
        page = first_page(context)
        page.goto(EARTH_STUDIO_URL)

        if "studio" not in page.url:
            context.close()
            sys.exit("error: not logged in -- run `uv run src/studio_login.py` first")

        print(gpu_status(page))

        page.click(IMPORT_TOGGLE)
        with page.expect_file_chooser() as file_chooser_info:
            page.click(IMPORT_MENU_ITEM)
        file_chooser_info.value.set_files(str(esp_file))
        page.wait_for_timeout(PROJECT_LOAD_WAIT_MS)

        page.click(RENDER_BUTTON)
        page.wait_for_timeout(1000)
        page.click(FORMAT_IMAGE_SEQUENCE_OPTION)

        select_output_folder(page, output_dir)
        click_permission_bubble()  # "Allow this site to view and copy files?"

        page.click(START_RENDER_BUTTON)
        click_permission_bubble()  # "Save changes to <folder>?"
        wait_for_frames(output_dir, expected_frame_count=expected_frame_count, timeout_ms=RENDER_TIMEOUT_MS)

        context.close()
        print(f"Rendered frames written to {output_dir}/")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    esp_data = validate_esp_file(args.esp_file)
    duration = esp_data.get("settings", {}).get("duration")
    # Confirmed live: a duration of 450 wrote exactly 450 frames
    # (frankfurt_hauptbahnhof_orbit_000.jpeg .. _449.jpeg), so the .esp's
    # settings.duration is already the true frame count.
    expected_frame_count = duration

    if args.dry_run:
        print(f"Would import {args.esp_file} into Earth Studio")
        print("Would set render format to Image sequence (.jpeg)")
        print(f"Would render frames directly into {args.output_dir}/")
        return

    render(args.esp_file, args.output_dir, headless=args.headless, expected_frame_count=expected_frame_count)


if __name__ == "__main__":
    main()
