"""Render an Earth Studio .esp project to a JPEG image sequence.

Drives the real earth.google.com/studio web app with Playwright: imports the
given .esp file, opens the Render panel (only local output format is an Image
Sequence of JPEGs -- there is no PNG or Cloud-free option), grants it
--output-dir as the destination via the native OS folder picker, triggers the
render, and waits for frames to show up in that directory. Encoding the
sequence into a video is a separate, later step -- not done here.

Requires a logged-in session -- run `earth login` first.

    uv run earth render frankfurt_hauptbahnhof_orbit.esp
    uv run earth render frankfurt_hauptbahnhof_orbit.esp --output-dir out/ --headless

Local Image Sequence rendering grants filesystem access via the browser's
File System Access API (window.showDirectoryPicker()), which Chromium serves
as a *native OS dialog* -- outside Playwright's DOM/CDP control entirely.
select_output_folder() below drives that dialog with xdotool instead, and
depends on the D-Bus/portal/window-manager stack docker/services.sh brings up
plus /dev/fuse + CAP_SYS_ADMIN (see the Dockerfile, run.sh / devcontainer.json,
and the Security section in README.md) -- without those, the dialog opens but
its confirmation hangs forever.

Picking the folder is followed by two more native prompts -- "Allow this site
to view and copy files?" (read) and "Save changes to <dir>?" (write) -- also
browser UI rather than page DOM. Playwright's bundled Chrome for Testing
ignores the enterprise policy that was meant to pre-grant these (it reads
policies from a path that build doesn't honor), so start_render() accepts
them from the keyboard, same as the folder picker.

Earth Studio's UI is an undocumented, JS-heavy SPA, so the selectors below are
best-effort and may need adjusting after the first live run -- this is the one
place to fix them.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Self

import typer
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Playwright, sync_playwright

from .browser import (
    EARTH_STUDIO_URL,
    close_quietly,
    first_page,
    gpu_status,
    launch_context,
)

# --- Earth Studio UI selectors (best-effort; fix here if a step fails) -----
IMPORT_TOGGLE = (
    "div.split-button:has(button[data-action='click:openProject']) button.toggle-dropdown"
)
IMPORT_MENU_ITEM = "div[data-action='click:importProject']"
RENDER_BUTTON = "text=Render"
FORMAT_IMAGE_SEQUENCE_OPTION = "text=Image sequence (.jpeg)"
CHOOSE_FOLDER_BUTTON = "#output-dir-btn"  # reads "Choose folder" only the first time; a remembered destination replaces the label with its name
CHOOSE_FOLDER_LABEL = "Choose folder"
START_RENDER_BUTTON = "button[data-action='click:onStartClick']"
# Closes the render panel/progress view once a render finishes, so a later
# render on the same warm page starts from a clean editor. Unverified guess
# (Session.render_esp is the first thing in this repo to render twice on one
# page) -- _close_render_panel() below falls back to Escape and never raises,
# so a wrong selector here degrades instead of breaking a render.
CLOSE_RENDER_PANEL_BUTTON = "button[data-action='click:onCloseClick']"
# -----------------------------------------------------------------------------

# Title of the Chrome-for-Testing window, for aiming keyboard input at the
# native File System Access prompts (which carry no window title of their own).
BROWSER_WINDOW_TITLE = "Google Earth Studio"

RENDER_TIMEOUT_MS = (
    3 * 60 * 60 * 1000
)  # SwiftShader software rendering (no real GPU here) is slow -- give it room

# Upper bound on how long an import may take to show up in the editor, and the
# blind wait wait_for_project_loaded() falls back to when it never does.
PROJECT_LOAD_WAIT_MS = 8000  # importing a project re-renders the whole scene
# Breathing room after the imported project's name appears, before touching the
# Render button. The name can land while the SPA is still rebuilding around it,
# and RENDER_BUTTON is present in the toolbar the whole time -- so Playwright's
# own actionability wait offers no protection against clicking too early.
SCENE_SETTLE_MS = 1500


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
        raise RuntimeError(
            "folder picker dialog never appeared -- check the D-Bus/portal/WM stack is running"
        )

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

    # "Select Folder" is the dialog's default button, so Return activates it
    # wherever GTK happens to have placed it. This used to click a fixed pixel
    # offset from the window's bottom-right corner instead, which silently
    # missed whenever the dialog's layout shifted -- and UI font metrics alone
    # are enough to shift it, so a devcontainer rebuild could break it.
    _xdotool("windowactivate", "--sync", wid)
    time.sleep(0.3)
    _xdotool("key", "--window", wid, "Return")

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if wid not in _xdotool("search", "--name", DIALOG_TITLE_PATTERN).splitlines():
            return
        time.sleep(0.5)
    raise RuntimeError(
        "folder picker did not close after confirming the output folder -- "
        "check /dev/fuse + CAP_SYS_ADMIN are granted (see devcontainer.json / README Security section)"
    )


def project_name_of(esp_file: Path) -> str:
    """The name Earth Studio shows for this project once imported -- what
    wait_for_project_loaded() watches for. settings.name is what the app
    actually displays; the filename is only a fallback for a hand-edited .esp
    whose two disagree.
    """
    try:
        return json.loads(esp_file.read_text())["settings"]["name"]
    except (OSError, ValueError, KeyError, TypeError):
        return esp_file.stem


def wait_for_project_loaded(
    page: Page, project_name: str | None, *, timeout_ms: int = PROJECT_LOAD_WAIT_MS
) -> None:
    """Wait for a just-imported project to appear in the editor.

    Importing re-renders the whole scene and Earth Studio fires no load event,
    so this used to be a blind sleep of the full timeout on every render --
    the single largest avoidable cost in `earth serve`'s per-request path. The
    project's own name showing up is the earliest signal that the import
    landed: `earth serve` names every per-request project uniquely (frame_0,
    frame_1, ...), so it is a fresh name each time.

    project_name is None when the caller found that name already on screen
    before importing (see Session.render_esp) -- then its presence proves
    nothing and only the blind wait is left. Same fallback if the name never
    shows: a UI change costs the old behaviour, not a broken render.
    """
    if project_name is None:
        page.wait_for_timeout(timeout_ms)
        return
    try:
        page.get_by_text(project_name).first.wait_for(timeout=timeout_ms)
    except PlaywrightError:
        return  # never appeared -- the wait above already burned the full timeout
    page.wait_for_timeout(SCENE_SETTLE_MS)


def _frames_on_disk(output_dir: Path) -> int:
    return len(list(output_dir.rglob("*.jpg"))) + len(list(output_dir.rglob("*.jpeg")))


# Where the grant button ("Allow" / "Save changes") sits when the browser fills
# the 1280x800 virtual display -- both prompts anchor just under the omnibox and
# their grant buttons overlap around this point. Used only as a fallback.
_GRANT_BUTTON_XY = ("780", "130")


def _accept_native_prompt(window_id: str | None, *, click_fallback: bool) -> None:
    """Accept a native File System Access prompt. Focus defaults to the deny
    button; one Tab reaches the grant button ("Allow" / "Save changes"), Enter
    confirms.

    Keyboard first, via XTEST (`xdotool key` with no `--window`) to the focused
    window -- Chromium's permission UI ignores the XSendEvent that `--window`
    uses, unlike the GTK folder picker. fluxbox is occasionally slow to actually
    focus the activated window, so once the keyboard path has missed a couple of
    times, also click the grant button at its fixed on-screen position.
    """
    if window_id is None:
        return
    _xdotool("windowactivate", "--sync", window_id)
    time.sleep(0.6)
    _xdotool("key", "--clearmodifiers", "Tab")
    time.sleep(0.4)
    _xdotool("key", "--clearmodifiers", "Return")
    time.sleep(0.6)
    if click_fallback:
        _xdotool("mousemove", *_GRANT_BUTTON_XY, "click", "1")
        time.sleep(0.6)
    time.sleep(0.6)


def _render_started(page: Page, output_dir: Path, baseline_frames: int) -> bool:
    """Whether the render is underway. Two independent signals, either suffices:
    a frame has landed on disk (unambiguous and selector-free), or the Start
    button has gone -- Earth Studio swaps the render dialog for its progress
    view once the render really begins.
    """
    if _frames_on_disk(output_dir) > baseline_frames:
        return True
    try:
        return not page.locator(START_RENDER_BUTTON).first.is_visible()
    except PlaywrightError:
        return True  # detached entirely -> the dialog is gone


def start_render(
    page: Page,
    output_dir: Path,
    *,
    expect_prompts: bool,
    prompt_grace_s: float = 5.0,
    max_prompts: int = 4,
    timeout_s: float = 120.0,
) -> None:
    """Click Start, and wait for the render to actually begin.

    Granting the output folder raises up to two native File System Access
    prompts -- "Allow this site to view and copy files?" then "Save changes to
    <dir>?" -- and each blocks the render until answered. They are browser
    chrome rather than page DOM, so _accept_native_prompt() clears them blind.

    But they only ever follow a *fresh* grant: Chromium remembers it against
    the profile, so every later render of the same folder -- including every
    `earth serve` request after the first -- sees no prompt at all. expect_prompts
    (threaded down from Session._folder_grant_needed()) says which case this is,
    and the warm path never touches the keyboard or hunts for an X window.

    Progress is judged by _render_started() rather than by the Start button
    going hidden. That mattered: Earth Studio keeps the button on screen right
    through "Preparing to render...", so waiting for it to hide always timed
    out at least twice, and the old loop's fixed 6s timeout plus ~3s of
    _accept_native_prompt() sleeps per attempt turned a ~10s prep into ~21s and
    fired stray Tab/Enter at the browser while it was working.
    """
    baseline_frames = _frames_on_disk(output_dir)
    # Only worth resolving when there may be a prompt to aim at: with no
    # display (headless) this search burns its whole timeout and finds nothing.
    window_id = _find_window(BROWSER_WINDOW_TITLE, timeout_s=10.0) if expect_prompts else None

    try:
        page.click(START_RENDER_BUTTON, timeout=5000)
    except PlaywrightError:
        return  # button already gone -> render is running

    deadline = time.monotonic() + timeout_s
    prompts_cleared = 0
    next_prompt_at = time.monotonic() + prompt_grace_s
    while time.monotonic() < deadline:
        if _render_started(page, output_dir, baseline_frames):
            return
        if expect_prompts and prompts_cleared < max_prompts and time.monotonic() >= next_prompt_at:
            _accept_native_prompt(window_id, click_fallback=prompts_cleared >= 1)
            prompts_cleared += 1
            next_prompt_at = time.monotonic() + prompt_grace_s
            # The prompt swallowed the first click -- Start is still there.
            try:
                page.click(START_RENDER_BUTTON, timeout=2000)
            except PlaywrightError:
                pass
        time.sleep(0.25)

    raise RuntimeError(
        f"render did not start within {timeout_s:.0f}s "
        f"({'prompts were cleared' if expect_prompts else 'no prompts expected'}) -- "
        "the prompt layout or Start selector may have changed"
    )


def _close_render_panel(page: Page) -> None:
    """Dismiss the render panel/progress view after a render finishes, so a later
    render on the same warm page starts from a clean editor. Best-effort and never
    raises. Worst case the panel is still open and the next render's clicks land
    on it instead -- fix the same way as any other selector here, by watching a
    live run.

    Escape goes first because it is instant and is what actually worked.
    CLOSE_RENDER_PANEL_BUTTON stays as a backstop but on a short timeout: it is
    an unverified guess, so on every render it most likely just waits out its
    own timeout matching nothing -- 3s of that per `earth serve` request was
    pure overhead.
    """
    try:
        page.keyboard.press("Escape")
    except PlaywrightError:
        pass
    try:
        page.click(CLOSE_RENDER_PANEL_BUTTON, timeout=800)
    except PlaywrightError:
        pass


def _print_progress(message: str) -> None:
    print(message, flush=True)


def wait_for_frames(
    output_dir: Path,
    *,
    expected_frame_count: int | None,
    timeout_ms: int,
    quiet_period_s: float = 60.0,
    poll_interval_s: float = 0.5,
    progress_interval_s: float = 30.0,
    progress: Callable[[str], None] = _print_progress,
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

    progress() is called with human-readable status lines instead of printing
    directly, so a caller that wants to stay quiet (e.g. one HTTP request among
    many on a long-running server) can pass a no-op.
    """
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    last_count = -1
    stable_since = None
    last_report = 0.0
    total = expected_frame_count or "?"
    while time.monotonic() < deadline:
        count = _frames_on_disk(output_dir)
        if expected_frame_count is not None and count >= expected_frame_count:
            progress(f"Rendered {count}/{total} frames")
            return
        if count != last_count:
            last_count = count
            stable_since = time.monotonic()
        elif (
            count > 0
            and stable_since is not None
            and time.monotonic() - stable_since >= quiet_period_s
        ):
            return
        # Without this the only output for up to three hours is silence, which
        # looks identical whether frames are landing slowly or not at all.
        now = time.monotonic()
        if now - last_report >= progress_interval_s:
            last_report = now
            progress(f"Rendered {count}/{total} frames ({now - started:.0f}s elapsed)")
        time.sleep(poll_interval_s)
    raise TimeoutError(
        f"no stable output detected in {output_dir} within the timeout (last count: {last_count})"
    )


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "out"


class NotLoggedInError(RuntimeError):
    """Raised when Earth Studio doesn't redirect to /studio -- no active session."""


def validate_esp_file(esp_file: Path) -> dict:
    if not esp_file.is_file():
        raise FileNotFoundError(f"{esp_file} does not exist")
    try:
        return json.loads(esp_file.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{esp_file} is not valid JSON: {e}") from e


class Session:
    """A warm, logged-in Earth Studio page. Reused across render_esp() calls so
    the browser launch, SPA load, and File System Access folder grant are each
    paid once instead of on every render -- what makes `earth serve` fast.
    """

    def __init__(self, playwright: Playwright, *, headless: bool = False):
        self.context = launch_context(playwright, headless=headless)
        # From here on the browser is running, so every exit -- a failed
        # login check, a Ctrl-C during the SPA load, which is where an
        # interrupt most often lands -- has to take it down with it.
        try:
            self.page = first_page(self.context)
            # Earth Studio can raise native window.confirm()/alert() dialogs (e.g.
            # "discard unsaved changes?" when importing over a loaded project) --
            # these are browser chrome, not page DOM, so this is the only way
            # Playwright can answer them. Registered once up front to cover every
            # render this session makes.
            self.page.on("dialog", lambda dialog: dialog.accept())
            self.page.goto(EARTH_STUDIO_URL)

            if "studio" not in self.page.url:
                raise NotLoggedInError("not logged in -- run `uv run earth login` first")

            print(gpu_status(self.page), flush=True)
        except BaseException:  # including KeyboardInterrupt
            self.close()
            raise
        self._granted_output_dir: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _folder_grant_needed(self, output_dir: Path) -> bool:
        if self._granted_output_dir != output_dir:
            return True
        try:
            label = self.page.locator(CHOOSE_FOLDER_BUTTON).inner_text(timeout=2000)
        except PlaywrightError:
            return True  # can't tell -- re-grant to be safe
        return label.strip() == CHOOSE_FOLDER_LABEL

    def render_esp(
        self,
        esp_file: Path,
        output_dir: Path,
        *,
        expected_frame_count: int | None,
        timeout_ms: int = RENDER_TIMEOUT_MS,
        progress: Callable[[str], None] = _print_progress,
    ) -> None:
        """Import esp_file and render it as a JPEG Image Sequence into output_dir.

        Safe to call more than once on the same Session -- each call imports
        over whatever project the page currently has loaded and only redoes
        the File System Access folder grant when output_dir has changed since
        the last call (see _folder_grant_needed()).
        """
        page = self.page

        # Checked before the import, because the name appearing *afterwards* is
        # only a load signal if it wasn't there to begin with -- a recents list
        # or a re-import of the same project would otherwise match instantly and
        # send us on to the Render button mid-load.
        project_name = project_name_of(esp_file)
        try:
            if page.get_by_text(project_name).count() > 0:
                project_name = None
        except PlaywrightError:
            project_name = None

        page.click(IMPORT_TOGGLE)
        with page.expect_file_chooser() as file_chooser_info:
            page.click(IMPORT_MENU_ITEM)
        file_chooser_info.value.set_files(str(esp_file))
        wait_for_project_loaded(page, project_name)

        page.click(RENDER_BUTTON)
        # No fixed wait before this one: unlike RENDER_BUTTON (permanently in
        # the toolbar), the format option only exists once the panel is open,
        # so Playwright's actionability wait is a real signal here.
        page.click(FORMAT_IMAGE_SEQUENCE_OPTION)

        # The native prompts start_render() may have to clear only follow a
        # fresh grant, so the two decisions are the same decision.
        grant_needed = self._folder_grant_needed(output_dir)
        if grant_needed:
            select_output_folder(page, output_dir)
            self._granted_output_dir = output_dir
        start_render(page, output_dir, expect_prompts=grant_needed)
        wait_for_frames(
            output_dir,
            expected_frame_count=expected_frame_count,
            timeout_ms=timeout_ms,
            progress=progress,
        )
        _close_render_panel(page)

    def close(self) -> None:
        """Shut the browser down. Idempotent, and safe on a session that is
        already dying -- it runs from the interrupt path too (see
        close_quietly()).
        """
        close_quietly(self.context)


def render(
    esp_file: Path, output_dir: Path, *, headless: bool, expected_frame_count: int | None
) -> None:
    with sync_playwright() as playwright, Session(playwright, headless=headless) as session:
        session.render_esp(
            esp_file,
            output_dir,
            expected_frame_count=expected_frame_count,
            timeout_ms=RENDER_TIMEOUT_MS,
        )
    # Only on success: an interrupt half-way through leaves however many frames
    # Earth Studio had already written, which is not a completed render.
    print(f"Rendered frames written to {output_dir}/", flush=True)


def command(
    esp_file: Annotated[Path, typer.Argument(help="The .esp project file to render.")],
    output_dir: Annotated[
        Path,
        typer.Option(
            help=(
                "Directory to render the JPEG sequence into (default: out/ at the repo "
                "root). Earth Studio nests frames under a project-name subfolder of this "
                "directory on its own."
            ),
        ),
    ] = DEFAULT_OUTPUT_DIR,
    headless: Annotated[
        bool,
        typer.Option(
            help="Run the browser headless (default: headed, until selectors are verified)."
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Validate the .esp file and print the planned actions without a browser."
        ),
    ] = False,
) -> None:
    """Render an Earth Studio .esp project to a JPEG image sequence."""
    try:
        esp_data = validate_esp_file(esp_file)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"error: {e}")

    duration = esp_data.get("settings", {}).get("duration")
    # Earth Studio renders the range inclusive of both ends: for
    # settings.duration 450 its own render dialog reads "Frames 0 - 450" and
    # "Rendered: n / 451". The earlier note here claimed duration was already
    # the true count, citing a run that produced _000..._449 -- but that run
    # stopped at 450 only because this waiter returned there and closed the
    # browser mid-write, losing the final frame.
    expected_frame_count = duration + 1 if duration is not None else None

    if dry_run:
        print(f"Would import {esp_file} into Earth Studio")
        print("Would set render format to Image sequence (.jpeg)")
        print(f"Would render frames directly into {output_dir}/")
        return

    try:
        render(
            esp_file,
            output_dir,
            headless=headless,
            expected_frame_count=expected_frame_count,
        )
    except NotLoggedInError as e:
        sys.exit(f"error: {e}")
