# Future ideas

Things considered and deliberately not built. Each one is here because the
reasoning is worth keeping — not because it is queued up.

## Render animations with the snapshot button instead of the Render panel

`earth render` drives Earth Studio's Render panel, which writes an image
sequence through the browser's File System Access API. Chromium serves that as
a **native OS dialog**, outside Playwright's reach, so `render.py` drives it
with `xdotool`: find the window by title, type the path into the location bar,
confirm, then clear two more native permission prompts from the keyboard with a
fixed-coordinate mouse click as a fallback.

`earth serve` already avoids all of this. It captures single frames with Earth
Studio's own snapshot button, which renders at the project's resolution and
arrives as an ordinary browser download — no output folder, no dialog, no
grant. If `earth render` stepped the timeline frame by frame and captured each
one the same way, it could drop:

- `select_output_folder()`, `_find_window()`, `_accept_native_prompt()`,
  `_GRANT_BUTTON_XY`, `start_render()`'s prompt-clearing loop,
  `_render_started()`, `wait_for_frames()`, `_folder_grant_needed()` — roughly
  350 lines of the most failure-prone code in the repo;
- `xdotool`, `xdg-desktop-portal`, `xdg-desktop-portal-gtk`, `dbus`, and the
  window manager from the container image and `docker/services.sh`;
- `/dev/fuse` and **`CAP_SYS_ADMIN`** from `run.sh` and the devcontainer — the
  capability most associated with container escapes, currently granted only so
  the portal can FUSE-mount its Documents export directory.

That is the largest single simplification available, and it would shrink the
container's privileged surface to almost nothing.

**Why it is not built.** A snapshot is one frame; an animation is not. It is
unverified whether snapshot output matches what the Render panel produces for
*animation* frames — motion blur and exact sub-frame timing are the specific
worries, and the Render panel is the path Earth Studio actually supports for
sequences. Per-frame download handling and playhead stepping would also need to
be reliable across hundreds of frames.

**What would settle it:** render a short clip both ways and compare pixel-wise,
the same method used to tune `SCENE_SETTLE_MS` (two runs of unchanged code
differ by 0.05–0.11 % of pixels — that is the noise floor; anything at that
level is equivalent). If the frames match, the whole apparatus above can go.

## Manage the container's services with a supervisor or a prebuilt base image

`docker/services.sh` brings up the display/VNC/portal stack by hand: each
process gets a `pgrep` guard, a `setsid ... </dev/null &` launch so it survives
the calling shell, and `sleep`-based sequencing between dependent services.

A process supervisor (`s6-overlay`, `supervisord`) or a base image that already
ships an Xvfb + noVNC desktop would replace most of it, and would handle
restart-on-crash and startup ordering properly rather than with fixed sleeps.

**Why it is not built.** The script works, is short, and is easy to read end to
end; a supervisor adds a configuration format and a layer of indirection for a
handful of processes that rarely fail. Worth revisiting only if the service list
grows or if the `sleep`-based ordering starts causing flaky starts.
