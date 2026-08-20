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
project, triggering its built-in Render panel as an Image Sequence (PNG), and
extracting the resulting frames. Turning that sequence into a video is a
separate, later step, not covered here.

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

This extracts the rendered PNG frames into `frankfurt_hauptbahnhof_orbit_frames/`
by default (`--output-dir` to override). Add `--dry-run` to validate the `.esp`
file and see the planned steps without opening a browser, or `--headless` once
the automation is verified to work unattended.

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
