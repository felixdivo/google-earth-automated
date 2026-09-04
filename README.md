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

Everything runs through one `earth` command; `uv run earth --help` lists the
subcommands (`build`, `login`, `render`, `gpu-check`, `serve`).

## Generating a project: `earth build`

`google_earth_automated/build.py` is both a conversion library (`build_orbit_esp`,
`destination_point`, `write_esp`, ...) and the `earth build` subcommand: give it
a point of interest and it builds a `.esp` project that orbits it, camera always
facing the target.

```
uv run earth build --lat 50.1070 --lon 8.6633 --ground-alt 115 \
    --radius 200 --camera-alt 400 --name frankfurt_hauptbahnhof_orbit
```

The `.esp` is written to `out/<name>.esp` by default (override with `-o`).

Every altitude is meters above sea level — `--ground-alt` describes the target,
`--camera-alt` places the camera, and neither is relative to the other. Earth
Studio clamps a camera that ends up below the terrain, so `--camera-alt` has to
clear the ground around the whole orbit, not just at the target.

Run `uv run earth build --help` for all options (waypoint count, leg
duration, frame rate, resolution, ...).

## Rendering a project to an image sequence

The easiest way to run this is `./run.sh` (see *Running with Docker* below) or
the devcontainer — both build the same image with the browser and display
stack already in place. To run it directly on your host instead, first
`uv run playwright install --with-deps chromium`, then:

```
uv run earth login
```

This opens a real, visible browser window against Earth Studio using a
persistent profile in `.auth/chrome-profile/` (gitignored — it holds live
Google session data). Log in there, then press Enter in the terminal; the
session is reused by every later render.

Then, for each project:

```
uv run earth render out/frankfurt_hauptbahnhof_orbit.esp
```

This imports the project, sets the render format to Image sequence (JPEG —
the only local, non-Cloud format Earth Studio offers), and renders the frames
directly into `out/frankfurt_hauptbahnhof_orbit/` by default (Earth Studio
nests frames under a project-name subfolder of `--output-dir` on its own).
Turning that sequence into a video is a separate, later step, not covered
here. The render always runs headed, on the virtual display: the native
folder picker it needs never appears in headless Chromium.

## Serving single frames: `earth serve`

For "what does it look like from here, facing that way" — one frame at a
time — rather than a full flyover:

```
uv run earth serve
curl -o /tmp/f.jpg \
    'http://localhost:8000/frame?lat=50.1070&lon=8.6633&alt=650&heading=180&tilt=70'
```

That second line doubles as a smoke test: point it at a server you are
debugging and it either leaves a viewable JPEG at `/tmp/f.jpg` or prints the
error the API returned.

`earth serve` keeps logged-in Earth Studio pages open for as long as it runs,
instead of launching a fresh browser per request the way `earth render` does.
`GET /frame` takes `lat`/`lon`/`alt` (camera position; `alt` in meters above
sea level), `heading` (compass bearing, default `0`) and `tilt` (degrees from
straight down, default `45`), and responds with the frame as `image/jpeg`;
`GET /health` answers `ok`. Frames are 1920x1080 — fixed for the life of the
server, because it is the one property of a frame that cannot be changed
without importing a new project.

Three things make it quick:

- **The project is imported once per worker, at startup.** A request types its
  camera pose straight into the editor's camera fields on the project already
  open, so it pays neither the import nor the whole-scene rebuild an import
  triggers.
- **Frames are captured with Earth Studio's own snapshot button**, not the
  Render panel: the same image at the project's resolution in half the time,
  usually byte-identical, arriving as an ordinary browser download. So
  `earth serve` needs no output folder and none of the native dialogs
  `earth render` has to drive — which is also why it runs headless.
- **`--workers N` runs a fleet.** N worker processes, each with its own browser
  and its own copy of the loaded project, sit behind one socket that
  [gunicorn](https://gunicorn.org/) load-balances across. A worker that dies is
  replaced; one that wedges past the frame timeout is killed and replaced.

How far that scales, and what stops it, is measured in
[docs/performance.md](docs/performance.md): parallelism is linear to 4 workers
and then flat, because the ceiling is Google's map-tile bandwidth rather than
anything on the machine. Poses that reuse tiles already streamed are not
subject to that ceiling and keep scaling well past 4. So pick `--workers` by
what you are rendering: 4 is the honest number for arbitrary poses, while a
workload that revisits the same area — a flyover, or many headings from one
point — goes much higher. Budget roughly 2.5GB of RAM per worker.

Each worker is a process rather than a tab because one Chromium may hold a
profile at a time, so each gets a throwaway copy of `.auth/chrome-profile/`
(~90MB; the 1.3GB tile cache is skipped), deleted when the server stops —
`earth login` still writes only the real profile. The copies share one Google
session, which was measured rather than assumed: up to 8 concurrent workers,
none was ever bounced to a sign-in page and the master profile's cookies stayed
valid. What to watch for as N grows is a re-auth challenge; the fix is `earth
login` again.

One fleet spreads over every GPU the container was given, so a second container
is not the unit of scaling. Chromium's Vulkan backend has no flag for picking a
GPU, but NVIDIA's userspace enumerates from the `/dev/nvidiaN` device nodes — so
each worker unshares its own mount namespace and binds `/dev/null` over the
nodes it should not use, leaving it exactly one card. Assignment is round-robin,
so a restarted worker returns to the same one. There is nothing to configure:
`GPUS=14,15 ./run.sh …` decides which GPUs exist and the fleet uses what it
finds; with one GPU, fewer than two workers, or no `CAP_SYS_ADMIN`, it says so
and leaves them unpinned. More GPUs will not raise throughput for arbitrary
poses — the ceiling there is tile bandwidth, not the GPU.

## Running with Docker

`./run.sh` builds the image (`Dockerfile`) and runs a command in it with the
repo bind-mounted, noVNC on port 6080, the `earth serve` API on port 8000,
and — on a Linux host with the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/)
— the GPU passed through via CDI for hardware-accelerated rendering:

```
./run.sh uv run earth gpu-check                  # verify the GPU is usable
./run.sh uv run earth login                      # one-time Google login (via noVNC)
./run.sh uv run earth build --lat ... --lon ...  # generate a project
./run.sh uv run earth render <project>.esp
./run.sh uv run earth serve --host 0.0.0.0       # single-frame render API on :8000
```

`earth serve`'s loopback-only default means *inside* the container -- real
Docker port publishing (`--publish`, not the devcontainer's own forwarding)
routes through the container's bridge network rather than its loopback, so
without `--host 0.0.0.0` there's nothing for `./run.sh`'s `--publish` to
reach at all. That publish is itself bound to the host's own `127.0.0.1`
(see `run.sh`), so the combination still only exposes
`http://localhost:8000` on the host, not the network.

The `GPUS` variable picks which GPUs to expose — a comma-separated list of CDI
device indices (`nvidia-ctk cdi list`), or `all`; the default is the last two.
With no GPU it falls back to software rendering. This does not need VS Code or
the devcontainer CLI; the devcontainer (which builds the same `Dockerfile` and
passes the same GPU devices) remains for that workflow.

## Technical details

Four things make this harder than a normal Playwright script, each explained
where it is dealt with rather than here:

- **The output folder is granted through a *native OS dialog*** (the File
  System Access API), outside Playwright's control — `render.py` drives it and
  the two permission prompts after it with `xdotool`, which is what the
  D-Bus/portal/window-manager stack and the `/dev/fuse` + `CAP_SYS_ADMIN` grant
  under [Security](#security) are for. `earth serve` avoids all of this by
  capturing with the snapshot button instead.
- **Earth Studio's UI is an undocumented, JS-heavy SPA**, so the selectors are
  best-effort and may need adjusting after a live run. They live in one block
  at the top of `google_earth_automated/render.py`.
- **A page can be imported into once**, because the import control exists only
  in the project browser and the editor has no route back — which is why
  `earth serve` re-poses one loaded project rather than importing per request.
- **Camera values are not `<input>` elements** but `.scrub-input` widgets, so
  they are clicked, typed into, and read back to confirm the value landed. A
  write that silently missed would render a plausible frame of the wrong place.

`render.py`'s module docstring and the comments around `select_output_folder()`
and `set_camera_pose()` carry the full reasoning, including the approaches that
did not work. Ideas considered and deliberately not built — chiefly retiring
that native-dialog path altogether — are in
[docs/future_ideas.md](docs/future_ideas.md).

Chromium's runtime shared libs and fonts come from Playwright's own
`--with-deps` list; the `Dockerfile` adds the display/VNC/portal stack and the
NVIDIA GL loaders on top. `docker/services.sh` (run by both `./run.sh`'s
entrypoint and the devcontainer's `postStartCommand`) starts a virtual display
+ noVNC on port 6080 — once the container is up, open
`http://localhost:6080/vnc.html` in a browser on your host to see and interact
with the Chromium window, which is needed for the headed `earth login` step
and the folder picker. Running natively on your host works too, without any of
this.

## GPU acceleration

Chromium renders Earth Studio's WebGL canvas on the GPU when one is available
and in software (SwiftShader) otherwise. Software is slower, but by less than
you might expect, because a large share of any short render is fixed browser
and UI overhead that the GPU does nothing for — see
[docs/performance.md](docs/performance.md) for the comparison. The gap matters
for long flyovers, not for a handful of frames.

`launch_context()` passes `--use-gl=angle --use-angle=vulkan
--ignore-gpu-blocklist`; on the render host that Vulkan path is the only combo
that engages the GPU (plain Chromium picks SwiftShader, `--use-angle=gl` lands
on Mesa llvmpipe — both CPU).

- **Linux host with an NVIDIA GPU** — `./run.sh` and the devcontainer both
  pass the GPU in via CDI (`--device=nvidia.com/gpu=…`); nothing else to
  configure. Pass several and `earth serve --workers N` spreads its fleet
  across them (see *Serving single frames*).
- **Windows (Docker Desktop, WSL2 backend)** — untested here; would need
  WSL2's `/dev/dxg` + `/usr/lib/wsl/lib` bind-mounted into the container and
  the ANGLE backend switched to `d3d12`.
- **Mac (Docker Desktop, any chip)** — not possible. Docker Desktop's VM uses
  `Hypervisor.framework`, which offers no GPU passthrough.

### How to verify

`uv run earth gpu-check` launches Chromium the same way `earth render` does
and prints its actual WebGL renderer — a quick check with no Earth Studio or
login involved. `earth render` prints the same line at startup, before the
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
- **`/dev/fuse` + `CAP_SYS_ADMIN`** (`runArgs` in `devcontainer.json`, and the
  same flags in `run.sh`) — needed twice over: so the native output-folder
  picker can confirm via `xdg-desktop-portal`'s FUSE-backed Documents portal,
  and so `earth serve --workers N` can unshare a mount namespace per worker to
  pin it to one GPU. `CAP_SYS_ADMIN` is the single capability most associated
  with real container-escape exploits. Dropping it costs the GPU spread (the
  fleet says so and runs unpinned) and `earth render` entirely.
- **Chromium's automation-detection flags are stripped**
  (`--disable-blink-features=AutomationControlled`, `--enable-automation`
  removed) — this is what gets past Google's bot-detection at login, but it
  also makes the browser indistinguishable from a real user session to any
  other site it visits.
- **File System Access grants persist in the profile** — `start_render()`
  auto-accepts Earth Studio's "view and copy files" / "save changes" prompts,
  and Chromium then remembers the grant for that folder. This profile exists
  only to drive Earth Studio; don't browse with it.
- **Chromium runs with `--no-sandbox`** — an automatic consequence of running
  as root, and it removes Chromium's own internal process-sandboxing against
  malicious content it renders.
- **`.auth/chrome-profile/` holds a live Google session** (gitignored, never
  committed) — anyone with filesystem access to it can reuse your login
  without re-authenticating.
- **`earth serve --workers N` copies the login profile N times** — each copy
  holds the same live Google session and lives in a temp directory for as long
  as the server runs (deleted on exit, killed hard and they linger). Anyone with
  filesystem access to them can reuse your login, same as for the profile
  itself.
- **`earth serve` is unauthenticated** — anyone who can reach its port can
  drive the logged-in Earth Studio session to render frames. It binds
  `127.0.0.1` by default for exactly this reason; only pass `--host 0.0.0.0`
  (needed under Docker, see *Running with Docker* above) on a trusted
  network, or put it behind your own auth/reverse proxy.

## License

Copyright (c) 2026 Felix Divo — [MIT](LICENSE).

Make sure your use complies with Google's Terms of Service.
