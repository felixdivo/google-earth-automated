"""Render an Earth Studio .esp project to a JPEG image sequence.

Drives the real earth.google.com/studio web app with Playwright: imports the
given .esp file, opens the Render panel (only local output format is an Image
Sequence of JPEGs -- there is no PNG or Cloud-free option), grants it
--output-dir as the destination via the native OS folder picker, triggers the
render, and waits for frames to show up in that directory. Encoding the
sequence into a video is a separate, later step -- not done here.

Requires a logged-in session -- run `earth login` first. Always runs headed
(on the virtual display) -- select_output_folder() below drives Chromium's
native folder picker with xdotool, which does not exist in headless Chromium.

    uv run earth render frankfurt_hauptbahnhof_orbit.esp
    uv run earth render frankfurt_hauptbahnhof_orbit.esp --output-dir out/

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
from contextlib import suppress
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
# Earth Studio's own "save this frame" button, in the top-right toolbar. It
# renders the current view at the *project's* resolution (not the viewport's)
# and hands it over as an ordinary browser download -- no render panel, no
# output folder, and none of the File System Access machinery an Image Sequence
# needs. That is what `earth serve` captures frames with; `earth render` still
# goes through the render panel, because a snapshot is one frame and an
# animation is not.
SNAPSHOT_BUTTON = "[data-action='click:snapshot']"
# -----------------------------------------------------------------------------

# --- The editor's camera attribute panel -------------------------------------
# Each camera attribute is one <li class="attribute" data-attribute-type="...">
# wrapping a .scrub-input: Earth Studio's drag-or-type number widget. It is not
# an <input> -- it displays a rounded .presentedValue and only swaps in a
# contenteditable <div class="input"> once clicked, which is why a value is set
# by clicking, selecting all and typing rather than with fill().
#
# Longitude and Pan carry *two* scrub-inputs: a whole-turns counter
# (.rotationInput, unit "x") and the degrees value (.valueInput). Only the
# second is meant here, hence the explicit .valueInput.
CAMERA_ATTRIBUTE = "li.attribute[data-attribute-type='{attr}'] .scrub-input.valueInput"
PRESENTED_VALUE = ".presentedValue"
# Pose component -> attribute type. rotationX/rotationY are labelled Pan and
# Tilt in the UI; rotationZ (roll) is left at whatever the project sets.
POSE_ATTRIBUTES = (
    ("latitude", "lat"),
    ("longitude", "lon"),
    ("altitude", "alt"),
    ("rotationX", "heading"),
    ("rotationY", "tilt"),
)
# An imported project opens with both groups expanded, but a collapsed group
# hides its rows without removing them -- the values stay readable and become
# impossible to click, so this is checked rather than assumed.
CAMERA_GROUP = "li.attribute-group:has(.title:text-is('{title}'))"
CAMERA_GROUP_TOGGLE = "[data-action='click:toggleExpanded']"
CAMERA_GROUP_TITLES = ("Camera Position", "Camera Rotation")
# -----------------------------------------------------------------------------

# Title of the Chrome-for-Testing window, for aiming keyboard input at the
# native File System Access prompts (which carry no window title of their own).
BROWSER_WINDOW_TITLE = "Google Earth Studio"


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


# Is the project name on screen anywhere the app might be showing it? Checked
# in the page rather than with a Playwright locator because get_by_text() --
# what this used to use -- only ever matches rendered *text nodes*, and an
# editor's project name is at least as likely to live in an editable title
# field, where a text-node match can never see it. That is what happened: the
# wait never fired once and burned its full 8s fallback on every render, so the
# saving it was written for was never actually realised.
#
# innerText, not textContent: textContent would also match a name hidden in an
# off-screen template or a recents list that has not been rendered yet, and
# firing early here means clicking Render into a half-loaded scene -- a wrong
# frame, which is far worse than a slow one.
_PROJECT_NAME_VISIBLE = """
(name) => {
    if (document.title.includes(name)) return true;
    for (const field of document.querySelectorAll('input, textarea')) {
        if ((field.value ?? '').includes(name)) return true;
    }
    return (document.body?.innerText ?? '').includes(name);
}
"""


def project_name_visible(page: Page, project_name: str) -> bool:
    """Whether project_name is showing right now. Never raises: a page that
    can't be probed (mid-navigation, execution context torn down) just counts
    as not showing it, which costs the blind wait rather than a render.
    """
    try:
        return bool(page.evaluate(_PROJECT_NAME_VISIBLE, project_name))
    except PlaywrightError:
        return False


# Upper bound on how long an import may take to show up in the editor, and the
# blind wait wait_for_project_loaded() falls back to when it never does.
PROJECT_LOAD_WAIT_MS = 8000  # importing a project re-renders the whole scene
# Breathing room after the imported project's name appears, before the caller
# starts working with the loaded scene -- clicking Render in `earth render`'s
# flow, or, for `earth serve`, posing the camera against the worker's warmup
# import. The name can land while the SPA is still rebuilding around it, and
# both RENDER_BUTTON and the camera fields are present in the DOM the whole
# time -- so Playwright's own actionability wait offers no protection against
# acting too early.
#
# 6s, not the 1500ms this started as, because Earth Studio streams its imagery
# in asynchronously and offers no "finished streaming" signal: the name appears
# long before the terrain has resolved, and rendering then produces a visibly
# coarser frame. Measured by rendering the same 10-frame project three ways and
# comparing pixels. Two runs of the unchanged code differ from each other by
# 0.05-0.11% of pixels (imagery tiles load slightly differently every time),
# which sets the noise floor. At 1500ms this code differed from that baseline by
# 3.2-8.0% -- 50-80x the floor, i.e. really rendering less detail. At 6000ms it
# is back to 0.06-0.10%, inside the floor.
#
# So most of what the project-name signal saves has to be given back to keep the
# output right: the whole wait lands near 6.7s against the 8s blind sleep it
# replaced. It is still the better shape -- a scene that settles quickly is no
# longer charged the full 8s -- but it is worth ~1.3s, not the ~6s it looks like
# with a short settle. Re-tune with the same pixel comparison, never by eye.
SCENE_SETTLE_MS = 6000


def wait_for_project_loaded(
    page: Page, project_name: str | None, *, timeout_ms: int = PROJECT_LOAD_WAIT_MS
) -> None:
    """Wait for a just-imported project to appear in the editor.

    Importing re-renders the whole scene and Earth Studio fires no load event,
    so this used to be a blind sleep of the full timeout on every import. The
    project's own name showing up is the earliest signal that the import
    landed, which is what this replaces the blind sleep with wherever
    import_project() runs: every `earth render` (a fresh import each time) and
    once per `earth serve` worker, at startup, when it loads its scratch
    project (see serve.load_project()). `earth serve` never imports again
    after that, so this wait is off its per-request path entirely -- a request
    there only moves the camera and takes a snapshot.

    project_name is None when the caller found that name already on screen
    before importing (see Session.import_project) -- then its presence proves
    nothing and only the blind wait is left. Same fallback if the name never
    shows: a UI change costs the old behaviour, not a broken render.

    Polled on a fixed interval rather than Playwright's default animation
    frame: innerText forces a layout, and the scene rebuild this is waiting on
    is exactly when the machine has no cycles to spare.
    """
    if project_name is None:
        page.wait_for_timeout(timeout_ms)
        return
    try:
        page.wait_for_function(
            _PROJECT_NAME_VISIBLE, arg=project_name, timeout=timeout_ms, polling=500
        )
    except PlaywrightError:
        return  # never appeared -- the wait above already burned the full timeout
    page.wait_for_timeout(SCENE_SETTLE_MS)


# How long one camera field gets to accept a value and show it back. Shared
# across every UI action that touches the camera panel or the snapshot button
# -- expanding a group, typing a field, reading one back, clicking snapshot --
# so it lives here rather than at each call site. These are on-screen widgets
# in an already-loaded editor, so this covers a slow frame, not a load --
# Playwright's 30s default would turn a moved selector into a half-minute hang
# per request.
POSE_FIELD_TIMEOUT_MS = 3000


def _expand_camera_groups(page: Page) -> None:
    """Make sure the camera attribute groups are open.

    A collapsed group keeps its rows in the DOM but hides them, so a value stays
    readable while becoming impossible to click -- which looks exactly like a
    broken selector three steps later. An imported project opens expanded, so
    this is normally two instant probes and no clicks.
    """
    for title in CAMERA_GROUP_TITLES:
        group = page.locator(CAMERA_GROUP.format(title=title)).first
        try:
            classes = (group.get_attribute("class", timeout=POSE_FIELD_TIMEOUT_MS) or "").split()
        except PlaywrightError:
            continue  # not on screen at all -- set_camera_pose() reports it better
        if "expanded" not in classes:
            with suppress(PlaywrightError):
                group.locator(CAMERA_GROUP_TOGGLE).first.click(timeout=POSE_FIELD_TIMEOUT_MS)


def _matches(shown: str, value: float) -> bool:
    """Does the widget's text agree with the value we asked for?

    Deliberately loose: the widget shows 3 decimals for degrees and none for
    metres, and rounds its own way (35.6595 comes back as "35.659"), so
    agreement is only required to within one displayed unit. That is enough for
    what this guards against -- a field that never took the value at all still
    holds a number whole degrees away.
    """
    try:
        return abs(float(shown) - value) <= 10 ** -len(shown.partition(".")[2])
    except ValueError:
        return False


def _type_camera_field(page: Page, attr: str, value: float) -> None:
    """Type one value into one scrub-input, without waiting to see it land.

    Clicking the widget swaps its display for a contenteditable holding the same
    text, so the value is replaced with select-all + type rather than fill().
    """
    field = page.locator(CAMERA_ATTRIBUTE.format(attr=attr)).first
    try:
        field.click(timeout=POSE_FIELD_TIMEOUT_MS)
    except PlaywrightError as e:
        raise RuntimeError(
            f"camera field {attr!r} is not on screen -- the editor may not have a "
            "project loaded, or the attribute panel's selectors have changed "
            "(see the camera panel block at the top of render.py)"
        ) from e
    page.keyboard.press("Control+a")
    page.keyboard.type(f"{value}")
    page.keyboard.press("Enter")


def _confirm_camera_fields(page: Page, wanted: dict[str, float]) -> None:
    """Wait until every field shows the value it was given, or say which didn't.

    Reading back is not optional. A click that misses, or a field that quietly
    refuses a value, leaves the previous number in place -- and the render that
    follows produces a perfectly plausible frame of the wrong place, which
    nothing downstream can detect.

    All five are checked in one polling loop rather than one after each write,
    because the widget re-renders asynchronously: reading straight after Enter
    returns the *old* number (an early draft read once and concluded, wrongly,
    that no write had ever worked). Waiting once for all five costs one field's
    latency instead of five.
    """
    deadline = time.monotonic() + POSE_FIELD_TIMEOUT_MS / 1000
    presented = {
        attr: page.locator(CAMERA_ATTRIBUTE.format(attr=attr)).first.locator(PRESENTED_VALUE).first
        for attr in wanted
    }
    shown: dict[str, str] = {}
    while True:
        pending = []
        for attr, value in wanted.items():
            try:
                shown[attr] = presented[attr].inner_text(timeout=POSE_FIELD_TIMEOUT_MS).strip()
            except PlaywrightError:
                shown[attr] = shown.get(attr, "")
            if not _matches(shown[attr], value):
                pending.append(attr)
        if not pending:
            return
        if time.monotonic() >= deadline:
            attr = pending[0]
            raise RuntimeError(
                f"camera field {attr!r} still reads {shown[attr]!r} after being set to "
                f"{wanted[attr]}"
            )
        page.wait_for_timeout(50)


def set_camera_pose(
    page: Page, *, lat: float, lon: float, alt: float, heading: float, tilt: float
) -> None:
    """Point the loaded project's camera at one pose by typing into the editor.

    This is what makes `earth serve` cheap. Changing five numbers on the project
    that is already open skips the import, the whole-scene rebuild an import
    triggers, and -- because Earth Studio prepares a page for rendering only once
    -- the ~18s "Preparing to render..." phase that dominates a first render.
    Measured on this host: a first render costs ~39s, every later one on the same
    page ~20s.

    Altitude is metres above sea level, heading is the compass bearing (Pan) and
    tilt is degrees from straight down, matching build_still_esp()'s arguments so
    the two paths describe a pose the same way.
    """
    _expand_camera_groups(page)
    values = {"lat": lat, "lon": lon, "alt": alt, "heading": heading, "tilt": tilt}
    wanted = {attr: values[key] for attr, key in POSE_ATTRIBUTES}
    for attr, value in wanted.items():
        _type_camera_field(page, attr, value)
    _confirm_camera_fields(page, wanted)


def _frames_on_disk(output_dir: Path) -> int:
    return len(list(output_dir.rglob("*.jpg"))) + len(list(output_dir.rglob("*.jpeg")))


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
        # Where the grant button ("Allow" / "Save changes") sits when the
        # browser fills the 1280x800 virtual display -- both prompts anchor
        # just under the omnibox and their grant buttons overlap around this
        # point.
        grant_button_xy = ("780", "130")
        _xdotool("mousemove", *grant_button_xy, "click", "1")
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
        # Same reasoning as wait_for_frames' poll interval: on a warm page this
        # loop is the only thing between clicking Start and the render running.
        time.sleep(0.1)

    raise RuntimeError(
        f"render did not start within {timeout_s:.0f}s "
        f"({'prompts were cleared' if expect_prompts else 'no prompts expected'}) -- "
        "the prompt layout or Start selector may have changed"
    )


def _close_render_panel(page: Page) -> None:
    """Dismiss the render panel after a render and wait for the editor to come
    back, so the next request can reach the camera fields.

    Escape does the closing -- a guessed close-button selector used to be tried
    as well, on an 800ms timeout, and being a guess it never matched and only
    ever spent those 800ms. What replaces it is a check with a purpose: the
    camera panel is hidden while the render panel is up, so waiting for one
    camera field to be visible again is exactly the condition the next pose
    write needs. Without it a client that requests frames back-to-back fails on
    the next write, several seconds away from the cause.

    Never raises. By the time this runs the frame is already on disk, and
    failing a request that produced a good frame would be the wrong trade -- if
    the editor really is stuck, the next write says so, naming the field.
    """
    with suppress(PlaywrightError):
        page.keyboard.press("Escape")
    with suppress(PlaywrightError):
        # Generous because it is only ever waited out when something is wrong:
        # the panel normally goes in well under a second.
        editor_return_timeout_ms = 10000
        page.locator(CAMERA_ATTRIBUTE.format(attr=POSE_ATTRIBUTES[0][0])).first.wait_for(
            state="visible", timeout=editor_return_timeout_ms
        )


def _print_progress(message: str) -> None:
    print(message, flush=True)


def wait_for_frames(
    output_dir: Path,
    *,
    expected_frame_count: int | None,
    timeout_ms: int,
    quiet_period_s: float = 60.0,
    # A directory scan is cheap and a finished frame is worth noticing quickly:
    # at 0.5s this added a quarter of a second, on average, to every `earth
    # serve` request -- real money against a request that now costs ~7s.
    poll_interval_s: float = 0.1,
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


# How long a snapshot may take to arrive. Measured at 0.4-4.1s depending on how
# much of the scene is already resident, so this is an outer bound on a slow
# scene rather than a number anything normally waits out.
SNAPSHOT_TIMEOUT_MS = 120000
# Outer bound on a whole `earth render` render. SwiftShader software rendering
# (no real GPU here) is slow -- give it room.
RENDER_TIMEOUT_MS = 3 * 60 * 60 * 1000


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

    def import_project(self, esp_file: Path) -> None:
        """Load esp_file into the editor, replacing whatever project is open.

        Split out from render_esp() because `earth serve` imports exactly one
        project and then never renders it through the panel -- it moves that
        project's camera and takes snapshots instead.
        """
        page = self.page

        # Checked before the import, because the name appearing *afterwards* is
        # only a load signal if it wasn't there to begin with -- a recents list
        # or a re-import of the same project would otherwise match instantly and
        # send us on mid-load. Same predicate as the wait itself, so the two
        # can't disagree about what "on screen" means.
        project_name = project_name_of(esp_file)
        if project_name_visible(page, project_name):
            project_name = None

        page.click(IMPORT_TOGGLE)
        with page.expect_file_chooser() as file_chooser_info:
            page.click(IMPORT_MENU_ITEM)
        file_chooser_info.value.set_files(str(esp_file))
        wait_for_project_loaded(page, project_name)

    def snapshot(self, *, timeout_ms: int = SNAPSHOT_TIMEOUT_MS) -> bytes:
        """Capture the current view with Earth Studio's snapshot button.

        Measured against rendering the same pose through the render panel: half
        the time (median 2.1s against 4.6s) for output that is usually
        byte-identical and never differs by more than the run-to-run cache
        noise. It also needs no output folder, which is why `earth serve` no
        longer touches the native folder picker at all.

        The download is read and deleted here rather than saved: the bytes are
        going straight into an HTTP response, and Playwright's temp copy would
        otherwise pile up for the life of the server.
        """
        with self.page.expect_download(timeout=timeout_ms) as download_info:
            self.page.click(SNAPSHOT_BUTTON, timeout=POSE_FIELD_TIMEOUT_MS)
        download = download_info.value
        path = download.path()
        try:
            return Path(path).read_bytes()
        finally:
            with suppress(PlaywrightError):
                download.delete()

    def capture_pose(
        self,
        *,
        lat: float,
        lon: float,
        alt: float,
        heading: float,
        tilt: float,
        timeout_ms: int = SNAPSHOT_TIMEOUT_MS,
    ) -> bytes:
        """Point the loaded project's camera at one pose and return that frame.

        The whole of an `earth serve` request: five numbers typed into the
        editor and a snapshot. No import, no scene rebuild, no render panel --
        and, deliberately, no wait for the imagery to finish streaming.

        That last one was measured, not assumed. Taking each pose cold with no
        wait at all produced images within 0.05% of the same pose taken after a
        full settle (max channel delta 31, i.e. inside the noise between two
        renders of one pose at different cache states), while the snapshot
        itself grew by exactly the time the wait would have taken -- 5.2s
        against 3.7s, 4.8s against 3.1s. The snapshot blocks until the scene has
        resolved, so waiting first only moves the cost around. If half-drawn
        frames ever do show up, that measurement is the thing to redo.
        """
        set_camera_pose(self.page, lat=lat, lon=lon, alt=alt, heading=heading, tilt=tilt)
        return self.snapshot(timeout_ms=timeout_ms)

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
        self.import_project(esp_file)

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


def render(esp_file: Path, output_dir: Path, *, expected_frame_count: int | None) -> None:
    # Always headed: select_output_folder() drives Chromium's native File
    # System Access folder picker with xdotool, which does not exist in
    # headless Chromium -- a first run headless always fails there.
    with sync_playwright() as playwright, Session(playwright) as session:
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

    try:
        render(
            esp_file,
            output_dir,
            expected_frame_count=expected_frame_count,
        )
    except NotLoggedInError as e:
        sys.exit(f"error: {e}")
