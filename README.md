# google-earth-automated

Generate Google Earth Studio `.esp` flyover project files programmatically.

`src/esp_builder.py` is both a conversion library (`build_orbit_esp`,
`destination_point`, `write_esp`, ...) and the `esp-builder` CLI: it takes a
point of interest and builds a `.esp` project that orbits it, camera always
facing the target.

```
uv run esp-builder --lat 50.1070 --lon 8.6633 --ground-alt 115 \
    --radius 200 --camera-height 30 --name frankfurt_hauptbahnhof_orbit
```

Run `uv run esp-builder --help` for all options (waypoint count, start
bearing, leg duration, frame rate, resolution, open vs. closed loop, ...).

The `.esp` format isn't officially documented by Google; this is a best-effort
implementation cross-referenced from community tooling and a real exported
project file.

## Rendering an .esp to an image sequence

Google Earth Studio has no public render API, so rendering is done by driving
the real `earth.google.com/studio` web app with Playwright: importing the
project and triggering its built-in Render panel as an Image Sequence (JPEG —
the only local render format Earth Studio offers). Turning that sequence into
a video is a separate, later step, not covered here.

One-time setup (the devcontainer already runs `playwright install chromium`
via `onCreateCommand`; run it yourself first if you're not using the
devcontainer):

```
uv run src/studio_login.py
```

`studio_login.py` opens a real, visible browser window against Earth Studio
using a persistent profile in `.auth/chrome-profile/` (gitignored — it holds
live Google session data). Log in there, then press Enter in the terminal;
the session is reused by every later render.

Then, for each project:

```
uv run src/studio_render.py frankfurt_hauptbahnhof_orbit.esp
```

This renders the JPEG frames directly into `out/frankfurt_hauptbahnhof_orbit/`
by default — Earth Studio nests frames under a project-name subfolder of
`--output-dir` (`out/` at the repo root by default) on its own. Add
`--dry-run` to validate the `.esp` file and see the planned steps without
opening a browser, or `--headless` once the automation is verified to work
unattended.

Earth Studio grants that output folder through the browser's File System
Access API, which Chromium serves as a *native OS dialog* — outside
Playwright's control entirely. `studio_render.py` drives that dialog with
`xdotool` instead (see `select_output_folder()`), which depends on the
devcontainer's D-Bus/portal/window-manager stack and on the `/dev/fuse` +
`CAP_SYS_ADMIN` grant described in the Security section below.

Earth Studio's UI is an undocumented, JS-heavy SPA, so `studio_render.py`'s
selectors are best-effort and may need adjusting after the first live run — the
Earth Studio interactions live in `src/earth_studio_browser.py` and the
selector block at the top of `src/studio_render.py`.

The devcontainer runs as root and installs Chromium's runtime shared libs
(libnss3, libatk1.0-0, ...) via `onCreateCommand` so `--headless` works out
of the box. For the headed `studio_login.py` step, the devcontainer also
starts a virtual display + noVNC (`.devcontainer/start-vnc.sh`, run via
`postStartCommand`) on port 6080 — once the container is up, open
`http://localhost:6080/vnc.html` in a browser on your host to see and
interact with the Chromium window. Running natively on your host works too,
without any of this.

## GPU acceleration

Rendering runs entirely in software by default — Chromium falls back to
SwiftShader because the devcontainer requests no GPU device. That's
deliberate: a hardcoded GPU device path that doesn't exist on the host would
fail to start the container outright, and `devcontainer.json` is shared
across whatever host you're on. The tradeoff is that renders are noticeably
slower here than the same project open in a real, GPU-accelerated browser on
your host.

Whether you can get real hardware acceleration instead depends on the host:

- **Windows (Docker Desktop, WSL2 backend)** — possible. WSL2 exposes the
  host GPU through `/dev/dxg` and Mesa's `d3d12` driver, which translates
  OpenGL/Vulkan calls into DirectX 12 run by the Windows GPU driver (works
  for NVIDIA/AMD/Intel alike, not vendor-locked). To try it, merge this into
  the existing `runArgs`/`mounts`/`containerEnv` entries in
  `devcontainer.json`:
  ```json
  "runArgs": ["--device=/dev/dxg"],
  "mounts": [
    {"source": "/usr/lib/wsl/lib", "target": "/usr/lib/wsl/lib", "type": "bind"}
  ],
  "containerEnv": {"LD_LIBRARY_PATH": "/usr/lib/wsl/lib"}
  ```
  Untested from this project's (Mac-based) development environment — verify
  it actually engages hardware rendering before relying on it, e.g. by
  checking that Chromium's process list no longer includes
  `--enable-unsafe-swiftshader`.
- **Mac (Docker Desktop, any chip)** — not possible today. Docker Desktop's
  VM uses `Hypervisor.framework`, which Apple gives no GPU passthrough or
  virtualization through at all.
- **Native Linux Docker host** — add `--device=/dev/dri` to `runArgs` to pass
  the host's render node straight through; no translation layer needed.

## Security

The devcontainer trades away several isolation defaults to make this
automation work; worth knowing before you run it:

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

This automates a Google product through its web UI rather than a public API;
make sure your use complies with [Google's Terms of Service](https://policies.google.com/terms).

## License

[MIT](LICENSE)
