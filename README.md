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

On a Linux host with an NVIDIA GPU, use `./run.sh` (see *Running with Docker*
below) and prefix each command with it. Otherwise, one-time setup (the
devcontainer already runs `uv run playwright install --with-deps chromium`
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

### Running with Docker

`./run.sh` builds the image (`Dockerfile`) and runs a command in it with the
repo bind-mounted, noVNC on port 6080, and — on a Linux host with the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/)
— the GPU passed through via CDI for hardware-accelerated rendering:

```
./run.sh uv run studio-gpu-check                    # verify the GPU is usable
./run.sh uv run studio-login                        # one-time Google login (via noVNC)
./run.sh uv run esp-builder --lat ... --lon ...     # generate a project
./run.sh uv run studio-render <project>.esp --headless
```

The `GPUS` variable picks which GPUs to expose (default: the last two — see
*The render host* under GPU acceleration); with none it falls back to software
rendering. This does not need VS Code or the devcontainer CLI —
`.devcontainer/` remains for that workflow.

### Technical details

Two things make this harder than a normal Playwright script:

- **Earth Studio grants the output folder through the browser's File System
  Access API**, which Chromium serves as a *native OS dialog* — outside
  Playwright's control entirely. `studio_render` drives it with `xdotool`
  instead (see `select_output_folder()`), which depends on the devcontainer's
  D-Bus/portal/window-manager stack and on the `/dev/fuse` + `CAP_SYS_ADMIN`
  grant described in Security below. Two more native prompts follow — "Allow
  this site to view and copy files?" and "Save changes to `<folder>`?" —
  which `start_render()` accepts from the keyboard (Tab to the grant button,
  Enter). A managed-policy file used to pre-grant these, but Playwright's
  bundled Chrome for Testing ignores enterprise policy, so the prompts really
  do appear; the grant then persists in the profile for later runs.
- **Earth Studio's UI is an undocumented, JS-heavy SPA**, so
  `studio_render`'s selectors are best-effort and may need adjusting after
  a live run — they live in `google_earth_automated/earth_studio_browser.py`
  and the selector block at the top of
  `google_earth_automated/studio_render.py`.

Chromium's runtime shared libs and fonts come from Playwright's own
`--with-deps` list, so `--headless` works out of the box; the devcontainer adds
just the display/VNC/portal stack on top. It starts a virtual display + noVNC
(`.devcontainer/start-vnc.sh`) on port 6080 for the headed `studio-login`
step — once the container is up, open `http://localhost:6080/vnc.html` in a
browser on your host to see and interact with the Chromium window. Running
natively on your host works too, without any of this.

## GPU acceleration

Chromium renders Earth Studio's WebGL canvas on the GPU when one is available
and in software (SwiftShader) otherwise — software is several times slower.
`launch_context()` passes `--use-gl=angle --use-angle=vulkan
--ignore-gpu-blocklist`; on the render host that Vulkan path is the only combo
that engages the GPU (plain Chromium picks SwiftShader, `--use-angle=gl` lands
on Mesa llvmpipe — both CPU).

- **Linux host with an NVIDIA GPU** — `./run.sh` (see *Running with Docker*
  above) passes the GPU in via CDI; nothing else to configure. The
  devcontainer requests no GPU, so it always renders in software.
- **Windows (Docker Desktop, WSL2 backend)** — untested here; would need
  WSL2's `/dev/dxg` + `/usr/lib/wsl/lib` bind-mounted into the container and
  the ANGLE backend switched to `d3d12`.
- **Mac (Docker Desktop, any chip)** — not possible. Docker Desktop's VM uses
  `Hypervisor.framework`, which offers no GPU passthrough.

### How to verify

`uv run studio-gpu-check` launches Chromium the same way `studio-render` does
and prints its actual WebGL renderer — a quick check with no Earth Studio or
login involved. `studio-render` prints the same line at startup, before the
render begins; either asks the page for its renderer rather than trusting
Chromium's launch flags, so it reflects the pipeline Earth Studio's canvas is
really drawn with:

```
GPU: not in use -- falling back to software rendering, expect a slow render (see README, 'GPU acceleration')
     renderer: ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 10.0.0) (0x0000C0DE)), SwiftShader driver)
```

A working GPU reports `GPU: in use, hardware-accelerated` instead, with a
renderer naming real hardware (`ANGLE (NVIDIA, Vulkan 1.4 (NVIDIA Tesla
V100...), NVIDIA)` via `./run.sh`, `ANGLE (Intel, Mesa ...)`, `D3D12 (...)`
under WSL2). Anything mentioning SwiftShader, llvmpipe, or softpipe is CPU
rasterization. For the full picture,
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
- **File System Access is pre-granted to every site**
  (`/etc/chromium/policies/managed/earth-studio.json`) — this browser profile
  will hand read/write access to a chosen folder to any page that asks,
  without prompting. It exists only to drive Earth Studio; don't browse with
  this profile.
- **Chromium runs with `--no-sandbox`** — an automatic consequence of running
  as root, and it removes Chromium's own internal process-sandboxing against
  malicious content it renders.
- **`.auth/chrome-profile/` holds a live Google session** (gitignored, never
  committed) — anyone with filesystem access to it can reuse your login
  without re-authenticating.

## License

Copyright (c) 2026 Felix Divo — [MIT](LICENSE).

Make sure your use complies with Google's Terms of Service.
