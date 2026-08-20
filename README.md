# google-earth-automated

Generate Google Earth Studio `.esp` flyover project files programmatically.

- `src/esp_builder.py` — conversion library: takes a look-at target and a list
  of camera waypoints (lat/lon/altitude) and builds a `.esp` project that
  orbits the waypoints while always facing the target.
- `src/generate_frankfurt_orbit.py` — demo: orbits Frankfurt Hauptbahnhof at
  ~200m and 30m up, producing `frankfurt_hauptbahnhof_orbit.esp`.

```
uv run src/generate_frankfurt_orbit.py
```

The `.esp` format isn't officially documented by Google; this is a best-effort
implementation cross-referenced from community tooling and a real exported
project file.
