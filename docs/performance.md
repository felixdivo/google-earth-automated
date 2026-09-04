# Performance

How fast `earth serve` is, how far it parallelizes, and what stops it.

## Summary

- **One worker renders a frame in about 6 seconds** at 1920×1080, and a lone
  `earth serve` sustains roughly **0.15 frames/s**.
- **`--workers` scales cleanly to 4, then stops.** What stops it is **Google's
  map-tile bandwidth, not this machine** — the fleet's aggregate tile download
  flattens at 7–8 MB/s and never rises again, however many workers are added.
- **Nothing local is saturated.** At 16 workers the fleet uses ~30 of 96 CPU
  cores, ~20% of each GPU, and ~5 GB of VRAM. There is no local knob worth
  tuning.
- **For poses that reuse tiles already streamed, it scales to 12× at 16
  workers** with latency almost flat. The ceiling is the network, so a workload
  that fetches less flies past it.
- **Pick `--workers 4`** for arbitrary poses; go higher only for workloads that
  revisit the same area (a flyover, or many headings from one point).

## Test setup

A container on a two-GPU host — Tesla V100-SXM3-32GB ×2, 96 CPU cores, 1.5 TB
RAM — rendering at 1920×1080. Except where noted, requests use poses far enough
apart to share no map tiles, which is the worst case and the honest one for an
API serving arbitrary coordinates.

## A single frame

A `GET /frame` takes **4.2–5.4 s** on an idle server, and a median of about
**6 s** when measured as part of a benchmark batch. That frame costs roughly
**13 CPU-seconds and 13 MB of downloaded map tiles**.

Two design choices set that baseline:

- **The project is imported once, at startup**, so a request pays neither the
  import nor the whole-scene rebuild it triggers — only five camera fields
  typed into a project already open.
- **Frames are captured with Earth Studio's snapshot button**, not the Render
  panel: the same image at the project's resolution in **median 2.1 s against
  4.6 s**, usually byte-identical.

The GPU is busy for only a small part of those 6 seconds — a single worker
leaves both cards under 10% utilization. Most of the frame is spent waiting for
map tiles to arrive and decoding them.

## Scaling with `--workers`, arbitrary poses

| `--workers` | throughput | speedup | median frame | tile download |
| ----------- | ---------- | ------- | ------------ | ------------- |
| 1           | 0.15 f/s   | 1.0×    | 6.3 s        | 2.1 MB/s      |
| 2           | 0.30 f/s   | 2.0×    | 6.5 s        | 4.2 MB/s      |
| 4           | 0.64 f/s   | 4.3×    | 6.4 s        | 7.2 MB/s      |
| 8           | 0.83 f/s   | 5.5×    | 8.3 s        | 7.2 MB/s      |
| 16          | 0.89 f/s   | 6.0×    | 12.8 s       | 7.8 MB/s      |

Linear to 4 workers, then flat — and past 4, adding workers only stretches
latency: much the same frames per second arrive, each one having taken twice as
long. Beyond 8 the differences are run-to-run noise on a throttle that varies
through the day, and the fleet is as likely to lose ground as to gain it, with
the slowest requests starting to hit the snapshot timeout. Nothing above 8 is
worth running for arbitrary poses.

## What sets the ceiling

Read the last column. The fleet's aggregate tile download flattens at 7–8 MB/s
from 4 workers on and never rises, while a single `curl` from the same
container pulls **78 MB/s** from Google. The pipe is wide open by an order of
magnitude; the tile stream is what is rationed.

A frame waits on that stream, because the snapshot blocks until the scene has
resolved. So past 4 workers, extra workers divide one fixed tile budget into
more, slower frames. Throughput is simply that budget divided by the tiles a
frame needs:

```
frames/s  ≈  aggregate tile bandwidth  ÷  MB of tiles per frame
```

which holds across every configuration tested — different worker counts,
different numbers of requests in flight, headed or headless, one GPU or two. At
16 workers the fleet spends ~33 CPU-seconds per frame, most of it *waiting*.

The cap is imposed upstream rather than by anything on this side, and it is a
throttle, not an error: tiles keep arriving, just slower. It is the expected
ceiling, not a sign that something is wrong.

## What the hardware can actually do

Re-render a pose the workers have already streamed, so the tiles are in memory
and nothing is fetched, and the same fleet scales the way the hardware suggests:

| `--workers` | throughput | speedup | median frame |
| ----------- | ---------- | ------- | ------------ |
| 1           | 0.36 f/s   | 1.0×    | 2.7 s        |
| 8           | 2.66 f/s   | 7.4×    | 2.9 s        |
| 16          | 4.43 f/s   | 12.3×   | 3.5 s        |

**12× at 16 workers, with latency almost flat.** The parallelism works; only
the network is rationed. A warm frame is also less than half the work of a cold
one — 2.7 s against 6.3 s — because the tile wait is most of a cold frame.

## What is not the bottleneck

Each of these was measured and ruled out:

- **CPU** — ~30 of 96 cores at 16 workers, about 2 cores per worker.
- **GPU** — ~20% utilization on each card and ~5 GB VRAM at 16 workers. One
  fleet spreads over every GPU in the container, but for arbitrary poses a
  second GPU does not raise throughput, because the first was never the limit.
- **The X server** — `Xvfb` is single-threaded and does saturate its one core
  at 12 or more headed workers, which looks like an answer until you remove it:
  `--headless` takes X out of the picture entirely and throughput does not
  improve. It would matter on a machine that was not already waiting on tiles.
- **Disk** — the browsers write their caches steadily (a couple of GB across a
  batch) but I/O wait stays at essentially zero.
- **Memory** — ~2.5 GB per worker, so even 16 workers want ~40 GB of the
  host's 1.5 TB.

## Choosing `--workers`

- **Arbitrary, scattered poses** — 4. Past that you buy latency, not frames.
- **Repeated or nearby poses** — as many as memory allows; this is the case
  that scales, and 16 workers were still improving.
- **Latency matters more than throughput** — fewer workers. A lone frame costs
  ~6 s whether the fleet holds one browser or sixteen.

## The one lever left

Each worker gets a copy of the login profile with `Default/Cache` — 1.3 GB of
map tiles for places already visited — skipped, so every worker starts cold.
Given that tile fetching is the entire ceiling, *copying* that cache per worker
is the remaining thing worth trying: it trades disk (and is nearly free on a
copy-on-write filesystem, which the copy already asks for via `--reflink=auto`)
for exactly the fetches that cap throughput. Sharing one cache directory
between browsers is not an option — Chromium rewrites cache entries in place,
so a shared directory or a hardlink farm corrupts.

## GPU versus software rendering

Chromium renders Earth Studio's WebGL canvas on the GPU when one is available
and in software (SwiftShader) otherwise. Software costs about **1.2×
end-to-end** — 95 s against 77 s for ten frames. The frames themselves render
1.4× slower and the first one takes **13.1 s against 4.4 s**, but a large share
of any short render is fixed browser and UI overhead the GPU does nothing for.
The gap matters for long flyovers, not for a handful of frames.
