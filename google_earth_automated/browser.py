"""Shared Playwright plumbing for driving the Google Earth Studio web app.

Uses a persistent browser profile (see PROFILE_DIR) so a login performed once
via `earth login` is reused by `earth render`. Headed by default: headless
Chromium needs extra GPU/WebGL flags to render Earth Studio's canvas correctly,
and the first live runs need to be visually debuggable anyway. A headed run
needs a display (X11/VNC forwarded into the container, or run natively on the
host) -- this devcontainer has no display/GPU by default.
"""

from contextlib import suppress
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright
from playwright.sync_api import Error as PlaywrightError

from . import REPO_ROOT

# The real profile, and the only one `earth login` ever writes. One Chromium may
# hold a profile at a time, so `earth serve` runs each of its workers on a
# throwaway copy and passes that copy as launch_context's profile_dir; every
# other command takes the default below.
PROFILE_DIR = REPO_ROOT / ".auth" / "chrome-profile"

EARTH_STUDIO_URL = "https://earth.google.com/studio"


def launch_context(
    playwright: Playwright, *, profile_dir: Path = PROFILE_DIR, headless: bool = False
) -> BrowserContext:
    """Launch (or reattach to) a persistent Earth Studio browser profile."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    # Chromium's singleton lock embeds the container hostname, which changes
    # on every devcontainer rebuild -- so a stale lock from a previous run
    # (e.g. the container being stopped without Chromium exiting cleanly)
    # always looks like another machine holds the profile. This profile is
    # only ever driven by one process at a time here, so it's safe to clear.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (profile_dir / name).unlink(missing_ok=True)
    return playwright.chromium.launch_persistent_context(
        profile_dir,
        headless=headless,
        # Google's sign-in flow detects the --enable-automation flag/infobar
        # Playwright adds by default and blocks login with "This browser or
        # app may not be secure." Hiding it gets past that check. (Real
        # Chrome, which is even less likely to get flagged, isn't an option:
        # Google doesn't ship Chrome for Linux ARM64, which this devcontainer
        # runs on.)
        # --test-type suppresses the "You are using an unsupported
        # command-line flag: --no-sandbox" infobar that Playwright's implicit
        # --no-sandbox (required to run Chromium as root) would otherwise
        # show. It shifts the page viewport down by its height and is pure
        # noise in the noVNC view of a headed run.
        #
        # The --use-angle=vulkan trio routes ANGLE (and so Earth Studio's
        # WebGL canvas) through the NVIDIA driver's Vulkan backend. On this
        # host that is the only combo that engages the GPU: plain Chromium
        # picks SwiftShader, and --use-angle=gl lands on Mesa llvmpipe -- both
        # CPU. Needs the GPU passed into the container (run.sh does this via
        # CDI); without a device it harmlessly falls back to SwiftShader.
        # --ignore-gpu-blocklist is required because Chromium blocklists the
        # headless GPU path by default.
        #
        # --hide-crash-restore-bubble: an unclean exit (a killed container
        # mid-render) otherwise leaves a "Restore pages?" bubble that steals
        # focus from the keyboard input the render command aims at the render
        # dialog's permission prompts.
        #
        # The four --disable-*background*/occlusion flags keep a window that is
        # not on top drawing at full speed. `earth serve --workers N` puts N
        # browser windows on one 1280x800 virtual display, so all but one are
        # always covered, and Chromium's default is to throttle a covered
        # window's timers and requestAnimationFrame -- which is exactly the loop
        # Earth Studio's canvas renders on, so a snapshot there would stall
        # instead of arriving.
        args=[
            "--disable-blink-features=AutomationControlled",
            "--test-type",
            "--use-gl=angle",
            "--use-angle=vulkan",
            "--enable-features=Vulkan",
            "--ignore-gpu-blocklist",
            "--hide-crash-restore-bubble",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion",
        ],
        ignore_default_args=["--enable-automation"],
    )


def first_page(context: BrowserContext) -> Page:
    """Return the context's initial tab, or open one if it has none."""
    return context.pages[0] if context.pages else context.new_page()


def close_quietly(context: BrowserContext) -> None:
    """Close a browser context on the way out, whatever state it is in.

    Every command closes its context from a `finally`, so this also runs on
    the interrupt path -- and there a Ctrl-C in a terminal went to the whole
    foreground process group, so Playwright's node driver and Chromium got it
    too and are already on their way out. Closing then raises a plain
    `Exception("Connection closed while reading from the driver")` rather than
    a PlaywrightError, hence the broad suppress: the browser being gone is the
    outcome this function wanted anyway, and a failure to close must never
    replace the interrupt that caused it.
    """
    with suppress(Exception):
        context.close()


# Renderer names Chromium reports when it fell back to CPU rasterization
# instead of a real GPU. Earth Studio is WebGL-bound, so hitting one of these
# is the difference between minutes and hours for a render.
SOFTWARE_RENDERERS = ("swiftshader", "llvmpipe", "softpipe", "software", "mesa offscreen")


def webgl_renderer(page: Page) -> str | None:
    """The renderer string Chromium actually gave this page's WebGL context,
    e.g. "ANGLE (NVIDIA, ...)" for a real GPU or a SwiftShader variant for the
    software fallback. None if no WebGL context could be created at all.

    Asking the page (rather than reading Chromium's launch flags) is what
    makes this trustworthy: the flags only say what was requested, while this
    is the pipeline Earth Studio's canvas will really be drawn with.
    """
    return page.evaluate(
        """() => {
            const gl = document.createElement('canvas').getContext('webgl2')
                    || document.createElement('canvas').getContext('webgl');
            if (!gl) return null;
            const info = gl.getExtension('WEBGL_debug_renderer_info');
            return info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
        }"""
    )


def gpu_status(page: Page) -> str:
    """Whether this page's WebGL is really GPU-accelerated, as a short report
    to print before a render starts.

    Never raises: Earth Studio redirects on load, so the probe can land in a
    destroyed execution context, and a diagnostic is not worth failing a
    render over.
    """
    # Retried because the redirect this races against is transient: the first
    # probe often lands in an execution context that is being torn down, and
    # the next one a moment later succeeds. A single attempt made the warning
    # below silently not appear on roughly every other run.
    last_error: PlaywrightError | None = None
    renderer = None
    for attempt in range(3):
        try:
            page.wait_for_load_state("domcontentloaded")
            renderer = webgl_renderer(page)
            break
        except PlaywrightError as e:
            last_error = e
            if attempt < 2:
                page.wait_for_timeout(1000)
    else:
        return f"GPU: could not be determined ({type(last_error).__name__})"

    if renderer is None:
        return "GPU: unavailable -- no WebGL context at all, Earth Studio will not render"
    if any(marker in renderer.lower() for marker in SOFTWARE_RENDERERS):
        return (
            "GPU: not in use -- falling back to software rendering, expect a slow "
            f"render (see README, 'GPU acceleration')\n     renderer: {renderer}"
        )
    return f"GPU: in use, hardware-accelerated\n     renderer: {renderer}"
