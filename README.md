# google-earth-automated

![Python](https://img.shields.io/badge/python-3.13%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Built with Vibes](https://img.shields.io/badge/built%20with-vibes-beige)

Generate Google Earth Studio `.esp` flyover projects programmatically, and
render them to JPEG image sequences without touching the UI by hand.

Google Earth Studio has no public API for either half, so both are automated
against the real product: `.esp` is Earth Studio's own (undocumented) project
file format, reverse-engineered here; rendering drives the real
`earth.google.com/studio` web app with Playwright, clicking through its
Render panel the same way a person would. **This automates a Google product
through its web UI — see [Security](#security) below before running it, and
make sure your use complies with
[Google's Terms of Service](https://policies.google.com/terms).**

## Generating a project: `esp-builder`

`google_earth_automated/esp_builder.py` is both a conversion library (`build_orbit_esp`,
`destination_point`, `write_esp`, ...) and the `esp-builder` CLI: give it a
point of interest and it builds a `.esp` project that orbits it, camera always
facing the target.

```
uv run esp-builder --lat 50.1070 --lon 8.6633 --ground-alt 115 \
    --radius 200 --camera-height 30 --name frankfurt_hauptbahnhof_orbit
```

Run `uv run esp-builder --help` for all options (waypoint count, start
bearing, leg duration, frame rate, resolution, open vs. closed loop, ...).

## Rendering a project to an image sequence

One-time setup (the devcontainer already runs `uv run playwright install chromium`
via `onCreateCommand`; run it yourself first if you're not using the
devcontainer):

```
uv run studio-login
```

This opens a real, visible browser window against Earth Studio using a
persistent profile in `.auth/chrome-profile/` (gitignored — it holds live
Google session data). Log in there, then press Enter in the terminal; the
session is reused by every later render.

Then, for each project:

```
uv run studio-render frankfurt_hauptbahnhof_orbit.esp
```

This imports the project, sets the render format to Image sequence (JPEG —
the only local, non-Cloud format Earth Studio offers), and renders the frames
directly into `out/frankfurt_hauptbahnhof_orbit/` by default (Earth Studio
nests frames under a project-name subfolder of `--output-dir` on its own).
Turning that sequence into a video is a separate, later step, not covered
here. Add `--dry-run` to validate the `.esp` file and see the planned steps
without opening a browser, or `--headless` once the automation is verified to
work unattended.

### Technical details

Two things make this harder than a normal Playwright script:

- **Earth Studio grants the output folder through the browser's File System
  Access API**, which Chromium serves as a *native OS dialog* and native
  permission bubbles — outside Playwright's control entirely.
  `studio_render` drives those with `xdotool` instead (see
  `select_output_folder()` and `click_permission_bubble()`), which depends on
  the devcontainer's D-Bus/portal/window-manager stack and on the
  `/dev/fuse` + `CAP_SYS_ADMIN` grant described in Security below.
- **Earth Studio's UI is an undocumented, JS-heavy SPA**, so
  `studio_render`'s selectors are best-effort and may need adjusting after
  a live run — they live in `google_earth_automated/earth_studio_browser.py`
  and the selector block at the top of
  `google_earth_automated/studio_render.py`.

The devcontainer installs Chromium's runtime shared libs so `--headless` works
out of the box, and starts a virtual display + noVNC
(`.devcontainer/start-vnc.sh`) on port 6080 for the headed `studio-login`
step — once the container is up, open `http://localhost:6080/vnc.html` in a
browser on your host to see and interact with the Chromium window. Running
natively on your host works too, without any of this.

## GPU acceleration

Rendering runs entirely in software by default (Chromium falls back to
SwiftShader) since the devcontainer requests no GPU device — a hardcoded
device path that doesn't exist on the host would fail to start the container
outright. This makes renders noticeably slower than the same project open in
a real, GPU-accelerated browser on your host. Whether you can fix that
depends on the host:

- **Windows (Docker Desktop, WSL2 backend)** — possible, via WSL2's `/dev/dxg`
  + Mesa `d3d12` translation layer. Merge into `devcontainer.json`:
  ```json
  "runArgs": ["--device=/dev/dxg"],
  "mounts": [
    {"source": "/usr/lib/wsl/lib", "target": "/usr/lib/wsl/lib", "type": "bind"}
  ],
  "containerEnv": {"LD_LIBRARY_PATH": "/usr/lib/wsl/lib"}
  ```
  Untested from this project's (Mac-based) development environment — verify
  it actually engages hardware rendering (Chromium's process list should no
  longer include `--enable-unsafe-swiftshader`) before relying on it.
- **Mac (Docker Desktop, any chip)** — not possible today. Docker Desktop's VM
  uses `Hypervisor.framework`, which Apple gives no GPU passthrough through at
  all.
- **Native Linux Docker host** — add `--device=/dev/dri` to `runArgs` to pass
  the host's render node straight through.

### How to verify

`studio-render` reports this at startup, before the render begins — it asks
the page for its actual WebGL renderer rather than trusting Chromium's launch
flags, so it reflects the pipeline Earth Studio's canvas is really drawn with:

```
GPU: not in use -- falling back to software rendering, expect a slow render (see README, 'GPU acceleration')
     renderer: ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 10.0.0) (0x0000C0DE)), SwiftShader driver)
```

A working GPU reports `GPU: in use, hardware-accelerated` instead, with a
renderer naming real hardware (`ANGLE (NVIDIA GeForce ...)`,
`ANGLE (Intel, Mesa ...)`, `D3D12 (...)` under WSL2). Anything mentioning
SwiftShader, llvmpipe, or softpipe is CPU rasterization. For the full picture,
open `chrome://gpu` in the noVNC-visible browser
(`http://localhost:6080/vnc.html`) and check the *Graphics Feature Status*
table.

## Security

The devcontainer trades away several isolation defaults to make the render
automation work. Worth knowing before you run it:

- **Runs as root** (`containerUser: root`) — a compromise of anything in the
  container gets root within it, not just an unprivileged user.
- **`/dev/fuse` + `CAP_SYS_ADMIN`** (`runArgs` in `devcontainer.json`) — needed
  so the native output-folder picker can confirm via `xdg-desktop-portal`'s
  FUSE-backed Documents portal; `CAP_SYS_ADMIN` is the single capability most
  associated with real container-escape exploits, though it stays scoped to
  Docker Desktop's own VM, not your host OS directly.
- **Chromium's automation-detection flags are stripped**
  (`--disable-blink-features=AutomationControlled`, `--enable-automation`
  removed) — this is what gets past Google's bot-detection at login, but it
  also makes the browser indistinguishable from a real user session to any
  other site it visits.
- **Chromium runs with `--no-sandbox`** — an automatic consequence of running
  as root, and it removes Chromium's own internal process-sandboxing against
  malicious content it renders.
- **`.auth/chrome-profile/` holds a live Google session** (gitignored, never
  committed) — anyone with filesystem access to it can reuse your login
  without re-authenticating.

## License

Copyright (c) 2026 Felix Divo — [MIT](LICENSE).

Make sure your use complies with Google's Terms of Service.
