"""Generate a Google Earth Studio .esp project: a camera orbit around Frankfurt
Hauptbahnhof, always facing it, ~200m away and ~30m above it. Rough demo numbers.
"""

from esp_builder import build_orbit_esp, destination_point, write_esp

# Frankfurt Hauptbahnhof, approximate coordinates and ground elevation (rough demo values).
HAUPTBAHNHOF_LAT = 50.1070
HAUPTBAHNHOF_LON = 8.6633
HAUPTBAHNHOF_GROUND_ALT_M = 115

ORBIT_RADIUS_M = 200
ORBIT_HEIGHT_M = 30

target = {
    "lat": HAUPTBAHNHOF_LAT,
    "lon": HAUPTBAHNHOF_LON,
    "alt_m": HAUPTBAHNHOF_GROUND_ALT_M,
}

waypoints = []
for bearing_deg in (0, 120, 240):
    lat, lon = destination_point(target["lat"], target["lon"], bearing_deg, ORBIT_RADIUS_M)
    waypoints.append({"lat": lat, "lon": lon, "alt_m": target["alt_m"] + ORBIT_HEIGHT_M})

esp = build_orbit_esp(
    target=target,
    waypoints=waypoints,
    name="frankfurt_hauptbahnhof_orbit",
    seconds_per_leg=5,
    close_loop=True,
)

write_esp(esp, "frankfurt_hauptbahnhof_orbit.esp")
print("Wrote frankfurt_hauptbahnhof_orbit.esp")
