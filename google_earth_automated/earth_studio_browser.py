"""Shared Playwright plumbing for driving the Google Earth Studio web app.

Uses a persistent browser profile (see PROFILE_DIR) so a login performed once
via studio-login is reused by studio-render. Headed by default: headless
Chromium needs extra GPU/WebGL flags to render Earth Studio's canvas correctly,
and the first live runs need to be visually debuggable anyway. A headed run
needs a display (X11/VNC forwarded into the container, or run natively on the
host) -- this devcontainer has no display/GPU by default.
"""

from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright
from playwright.sync_api import Error as PlaywrightError

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".auth" / "chrome-profile"

EARTH_STUDIO_URL = "https://earth.google.com/studio"


def launch_context(playwright: Playwright, *, headless: bool = False) -> BrowserContext:
    """Launch (or reattach to) the persistent Earth Studio browser profile."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # Chromium's singleton lock embeds the container hostname, which changes
    # on every devcontainer rebuild -- so a stale lock from a previous run
    # (e.g. the container being stopped without Chromium exiting cleanly)
    # always looks like another machine holds the profile. This profile is
    # only ever driven by one process at a time here, so it's safe to clear.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (PROFILE_DIR / name).unlink(missing_ok=True)
    return playwright.chromium.launch_persistent_context(
        PROFILE_DIR,
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
        # show -- left up, it pushes every native permission-bubble button in
        # studio-render's fixed click coordinates down by its height.
        args=["--disable-blink-features=AutomationControlled", "--test-type"],
        ignore_default_args=["--enable-automation"],
    )


def first_page(context: BrowserContext) -> Page:
    """Return the context's initial tab, or open one if it has none."""
    return context.pages[0] if context.pages else context.new_page()


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
    try:
        page.wait_for_load_state("domcontentloaded")
        renderer = webgl_renderer(page)
    except PlaywrightError as e:
        return f"GPU: could not be determined ({type(e).__name__})"

    if renderer is None:
        return "GPU: unavailable -- no WebGL context at all, Earth Studio will not render"
    if any(marker in renderer.lower() for marker in SOFTWARE_RENDERERS):
        return (
            "GPU: not in use -- falling back to software rendering, expect a slow "
            f"render (see README, 'GPU acceleration')\n     renderer: {renderer}"
        )
    return f"GPU: in use, hardware-accelerated\n     renderer: {renderer}"
