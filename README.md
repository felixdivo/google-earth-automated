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

Run `uv run earth build --help` for all options (waypoint count, start
bearing, leg duration, frame rate, resolution, open vs. closed loop, ...).

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
here. Add `--dry-run` to validate the `.esp` file and see the planned steps
without opening a browser. The render runs headed (on the virtual display) —
the native folder picker it needs never appears in headless Chromium.

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

`earth serve` starts a small HTTP API and keeps logged-in Earth Studio
pages open for as long as it runs, instead of launching a fresh browser per
request the way `earth render` does. `GET /frame` takes `lat`/`lon`/`alt`
(camera position; `alt` in meters above sea level), `heading` (compass
bearing, default `0`) and `tilt` (degrees from straight down, default `45`),
and responds with the frame as `image/jpeg` — measured at 4.2–5.4s per
request on a V100 at 1920x1080, most of it Earth Studio drawing. `GET /health`
reports how many workers are ready.

Frame size is set once per server with `--width`/`--height` rather than per
request: it is the one property of a frame that cannot be changed without
importing a new project, which would cost more than the option is worth.

The server keeps no frames on disk — nothing is written to `out/` or anywhere
else in the repo — and, since there's no auth on this endpoint and it drives a
logged-in Google session, it binds `127.0.0.1` by default (`--host` to change
that). See `uv run earth serve --help` for the full option list, and *Running
with Docker* below for reaching it from the host when running in a container.

Three things make it quick. Two avoid re-doing per request what Earth Studio
only needs done once:

- **The project is imported once, at startup.** A request types its camera
  pose straight into the editor's camera fields (Latitude, Longitude,
  Altitude, Pan, Tilt) on the project already open, so it pays neither the
  import nor the whole-scene rebuild an import triggers.
- **Frames are captured with Earth Studio's own snapshot button**, not the
  Render panel. It produces the same image at the project's resolution in half
  the time (median 2.1s against 4.6s, usually byte-identical), and it arrives
  as an ordinary browser download — so `earth serve` needs no output folder,
  and none of the native File System Access dialogs described under *Technical
  details*. `earth render` still uses the Render panel, because a snapshot is
  one frame and an animation is not.

And the third answers several requests at a time:

- **`--workers N` runs a fleet.** One browser answering one request at a time
  leaves the machine idle. N worker processes, each with
  its own browser and its own copy of the loaded project, sit behind one
  dispatcher on the public port: `GET /frame` goes to a free worker, and a
  request that finds none waits — the queue is the backpressure, so a client
  can fire as many requests as it likes without picking a worker itself.
  `GET /health` reports `ok - <ready>/<N> workers ready`, and a worker whose
  browser dies is rebuilt in the background while the rest keep serving.

Measured on two V100s at 1920x1080, over poses far enough apart to share no map
tiles:

| `--workers` | throughput | speedup | median frame | tile download |
| ----------- | ---------- | ------- | ------------ | ------------- |
| 1           | 0.15 f/s   | 1.0×    | 6.3s         | 2.1 MB/s      |
| 2           | 0.30 f/s   | 2.0×    | 6.5s         | 4.2 MB/s      |
| 4           | 0.64 f/s   | 4.3×    | 6.4s         | 7.2 MB/s      |
| 8           | 0.83 f/s   | 5.5×    | 8.3s         | 7.2 MB/s      |
| 16          | 0.89 f/s   | 6.0×    | 12.8s        | 7.8 MB/s      |

Scaling is linear to 4 workers and then stops, and what stops it is **Google's
map tiles, not this machine**. Look at the last column: the fleet's aggregate
tile download flattens at 7-8 MB/s from 4 workers on and never rises again,
while a single `curl` from this container pulls 78 MB/s from Google. A frame
waits on that, because the snapshot blocks until the scene has resolved — so
past 4 workers, extra workers divide the same tile bandwidth into more, slower
frames. At 16 the fleet is spending ~33 CPU-seconds per frame mostly *waiting*.

The counter-test: re-render one pose the workers have already streamed, so the
tiles are in memory and nothing is fetched. Then it scales the way the hardware
suggests — 0.36 f/s at 1 worker (median 2.7s), 2.66 at 8 (2.9s), 4.43 at 16
(3.5s): **12× at 16 workers, with latency almost flat**.

So nothing local is the ceiling, and there is no point tuning one. At 16
workers the fleet uses ~30 of 96 cores, ~20% of each GPU and ~5GB of VRAM;
running `--headless`, which removes the X server from the picture entirely,
does not improve throughput. (The single-threaded `Xvfb` does saturate its one
core at 12+ headed workers, and would matter on a machine that was not already
waiting on tiles.) Pick `--workers` by what you are rendering: 4 is the honest
number for arbitrary poses, while a workload that revisits the same area — a
flyover, or many headings from one point — keeps scaling well past it.

Each worker is a separate process rather than a tab, because one Chromium may
hold a profile at a time: every worker gets its own throwaway copy of
`.auth/chrome-profile/` in a temp dir (~90MB and ~0.1s each — `Default/Cache`,
1.3GB of map tiles for places already visited, is skipped, and there is no way
to *share* a cache between browsers: Chromium rewrites its cache entries in
place, so a shared directory or a hardlink farm corrupts). The copies are
deleted when the server stops, and `earth login` still writes only the real
profile. Skipping that cache means every worker starts cold, which given the
measurements above is the one lever left worth pulling: *copying* it per worker
(1.3GB each, free on a CoW filesystem — the copy already asks for
`--reflink=auto`) would trade disk for exactly the tile fetches that cap
throughput.

Copies work because they carry the same logged-in Google session, and that was
measured rather than assumed: at up to 8 concurrent workers, no worker was ever
bounced to a sign-in page, and the master profile's cookies came back
byte-identical and still valid after every run — each copy rotates its own
session cookies without invalidating the original. Google does not serialize an
account, and local snapshot rendering is client-side WebGL, so extra workers add
map-tile traffic rather than server-side render quota. What to watch for as N
grows is a re-auth challenge (fix: `earth login` again); slower tiles you will
find first, and that is the throughput ceiling measured above rather than a
sign of anything wrong.
One fleet spreads over every GPU the container was given, so a second container
is not the unit of scaling. Chromium's Vulkan backend has no flag for picking a
GPU — it takes the first device the driver enumerates, which would put every
worker on the same card — but NVIDIA's userspace enumerates from the
`/dev/nvidiaN` device nodes, and a mount namespace makes those a per-process
matter. Each worker is therefore started under `unshare --mount` with
`/dev/null` bound over the nodes it should not use, and sees exactly one GPU;
the binds are private, so they are invisible to the rest of the container and
vanish with the worker. Workers are assigned round-robin by index, so a
restarted one comes back to the same card.

There is nothing to configure: `GPUS=14,15 ./run.sh …` decides which GPUs exist,
and the fleet uses what it finds. With one GPU, fewer than two workers, or no
`CAP_SYS_ADMIN` to unshare with, it says so and falls back to leaving the
workers unpinned. More than two GPUs will not raise throughput for arbitrary
poses — the ceiling above is tile bandwidth, not GPU and not RAM (~2.5GB per
worker, so 16 workers want ~40GB of the host's 1.5TB).

### Running with Docker

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

### Technical details

Two things make this harder than a normal Playwright script:

- **Earth Studio grants the output folder through the browser's File System
  Access API**, which Chromium serves as a *native OS dialog* — outside
  Playwright's control entirely. `render.py` drives it with `xdotool`
  instead (see `select_output_folder()`), which depends on the
  D-Bus/portal/window-manager stack `docker/services.sh` brings up and on the
  `/dev/fuse` + `CAP_SYS_ADMIN` grant described in Security below. Two more
  native prompts follow — "Allow this site to view and copy files?" and "Save
  changes to `<folder>`?" — which `start_render()` accepts from the keyboard
  (Tab to the grant button, Enter). A managed-policy file used to pre-grant
  these, but Playwright's bundled Chrome for Testing ignores enterprise
  policy, so the prompts really do appear; the grant then persists in the
  profile for later runs.
- **Earth Studio's UI is an undocumented, JS-heavy SPA**, so
  `render.py`'s selectors are best-effort and may need adjusting after
  a live run — they live in `google_earth_automated/browser.py`
  and the selector block at the top of
  `google_earth_automated/render.py`.
- **The import control only exists in the project browser.** Once a project is
  open the app is in the editor, which has no import control and no route back
  — so a page can be imported into once and must then be re-used in place.
  That is why `earth serve` moves the camera of one loaded project instead of
  importing a project per request.
- **Camera values are not `<input>` elements.** Each one is a `.scrub-input`
  widget showing a rounded value, which swaps in a contenteditable only once
  clicked — hence click, select-all, type, and read the value back to confirm
  it landed (`set_camera_pose()`). A write that silently misses would render a
  plausible frame of the wrong place, which nothing downstream could detect.

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
and in software (SwiftShader) otherwise. Measured on a Tesla V100 at
1920×1080, software costs about 1.2× end-to-end (95s vs 77s for ten frames) —
the frames themselves render 1.4× slower, and its first frame takes 13.1s
against the GPU's 4.4s, but a large share of any short render is fixed
browser and UI overhead that the GPU does nothing for. The gap matters for
long flyovers, not for a handful of frames.

`launch_context()` passes `--use-gl=angle --use-angle=vulkan
--ignore-gpu-blocklist`; on the render host that Vulkan path is the only combo
that engages the GPU (plain Chromium picks SwiftShader, `--use-angle=gl` lands
on Mesa llvmpipe — both CPU).

- **Linux host with an NVIDIA GPU** — `./run.sh` and the devcontainer both
  pass the GPU in via CDI (`--device=nvidia.com/gpu=…`); nothing else to
  configure. Pass several and `earth serve --workers N` spreads its fleet
  across them (see *Serving frames over HTTP*).
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
