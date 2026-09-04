"""Conversion tool: coordinates + view direction/height -> Google Earth Studio .esp project.

The .esp format is not officially documented by Google. This module's structure
and value-encoding formulas were cross-referenced from two independent community
implementations and a real Earth-Studio-exported template, so it should open
correctly but may need small manual tweaks in-app.

Also doubles as the `earth build` subcommand for generating a circular orbit
.esp around a single point of interest.
"""

import json
import math
from pathlib import Path
from typing import Annotated

import typer

from . import REPO_ROOT

EARTH_RADIUS_M = 6371000.0

LON_RANGE = (-180.0, 180.0)
LAT_RANGE = (-90.0, 90.0)
TILT_RANGE = (0.0, 180.0)

# Altitudes are written as a plain fraction of Earth Studio's full-scale
# altitude, with no minValueRange/maxValueRange alongside them. The .esp field
# is a fraction whether we like it or not -- writing metres into it gets the
# value clamped (145 m came back as 65117 m) -- but nothing else about it needs
# to be relative, and the range keys this used to emit were actively harmful:
# Earth Studio applies minValueRange to latitude and longitude but *ignores* it
# for altitude, so the old (-500, ...) range silently lifted every camera by
# 500 m. Measured by importing built projects and reading the editor back: 145 m
# arrived as 645 m, 5000 m as 5500 m. With a bare fraction they arrive as asked,
# to within the ~1 m the app rounds off.
ALTITUDE_SPAN_M = 65117481.0


def bearing_between(lat1, lon1, lat2, lon2):
    """Compass bearing in degrees [0, 360) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def ground_distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two points (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def destination_point(lat, lon, bearing_deg, distance_m):
    """Point reached by travelling distance_m from (lat, lon) along bearing_deg."""
    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)
    theta = math.radians(bearing_deg)
    delta = distance_m / EARTH_RADIUS_M

    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lambda2) + 540) % 360 - 180


def _relative(value, value_range):
    lo, hi = value_range
    return (value - lo) / (hi - lo)


def _altitude(metres):
    """Metres above sea level, as the .esp's fraction of full scale."""
    return metres / ALTITUDE_SPAN_M


def _unwrap_headings(headings):
    """Make a list of compass headings continuous (no 359->1 style jumps)."""
    unwrapped = [headings[0]]
    for h in headings[1:]:
        prev = unwrapped[-1]
        candidates = (h, h - 360, h + 360)
        unwrapped.append(min(candidates, key=lambda c: abs(c - prev)))
    return unwrapped


def _tilt_for(cam, target):
    horizontal = ground_distance_m(cam["lat"], cam["lon"], target["lat"], target["lon"])
    vertical = target["alt_m"] - cam["alt_m"]
    elevation_angle_deg = math.degrees(math.atan2(-vertical, horizontal))
    return min(180.0, max(0.0, 90.0 - elevation_angle_deg))


def _block(attr_type, value, value_range=None, times=None, values=None, altitude=False):
    """A camera attribute, either static (one `value` for the whole scene) or
    keyframed (animated through `values` at `times`, with `value` as the initial
    pose). `altitude` marks the altitude/altitudePOI blocks, which -- unlike every
    other attribute type -- always carry an explicit `logarithmic: False`, even
    though it is never anything else (see ALTITUDE_SPAN_M above for why altitude
    is handled specially at all)."""
    block_value = {"relative": value if value_range is None else _relative(value, value_range)}
    if value_range is not None:
        block_value["minValueRange"], block_value["maxValueRange"] = value_range
    if altitude:
        block_value["logarithmic"] = False

    block = {"type": attr_type, "value": block_value}
    if times is not None:
        block["keyframes"] = [
            {"time": t, "value": v if value_range is None else _relative(v, value_range)}
            for t, v in zip(times, values)
        ]
        block["inTimeline"] = True
    return block


def _camera_lens_group():
    return {
        "type": "cameraLensGroup",
        "attributes": [
            {"type": "fov", "value": {}},
            {"type": "exposure", "value": {}},
            {"type": "aperture", "value": {}},
            {"type": "minFocusLength", "value": {}},
        ],
    }


def _environment_group():
    return {
        "type": "environmentGroup",
        "attributes": [
            {
                "type": "sunGroup",
                "attributes": [
                    {"type": "sunVisibility", "value": {}},
                    {"type": "worldTime", "value": {"relative": 0.5}},
                ],
            },
            {
                "type": "cloudGroup",
                "attributes": [
                    {"type": "cloudVisibility", "value": {}},
                    {"type": "cloudopacity", "value": {}},
                    {"type": "cloudheight", "value": {}},
                    {"type": "clouddate", "value": {"relative": 0.9545454545454546}},
                ],
            },
            {"type": "starsPlanetsGroup", "attributes": [{"type": "starsEnabled", "value": {}}]},
            {
                "type": "seawaterGroup",
                "attributes": [
                    {"type": "seawater", "value": {}},
                    {"type": "influence", "value": {"relative": 1}},
                ],
            },
            {"type": "buildingsEnabled", "value": {}},
        ],
    }


def _esp(scene, *, name, frame_rate, width, height, duration_frames):
    return {
        "modelVersion": 17,
        "settings": {
            "name": name,
            "frameRate": frame_rate,
            "dimensions": {"width": width, "height": height},
            "duration": duration_frames,
            "timeFormat": "frames",
        },
        "scenes": [scene],
        "playbackManager": {"range": {"start": 0, "end": duration_frames}},
    }


def _pan_range(heading):
    """Earth Studio stores rotationX relative to a min/max range, which cannot be
    degenerate -- a constant heading gets a half-degree window centred on it."""
    return (heading - 0.5, heading + 0.5)


def _scene(*, duration_frames, camera_group_attrs, camera_group_in_timeline):
    """The scene-dict skeleton shared by orbit and still projects: a single
    cameraGroup (position/rotation/target-effect attributes as given, plus the
    always-empty lens group) and the environment group, wrapped with the
    animationModel/cameraExport boilerplate Earth Studio expects on every scene."""
    camera_group = {"type": "cameraGroup"}
    if camera_group_in_timeline:
        camera_group["inTimeline"] = True
    camera_group["attributes"] = [*camera_group_attrs, _camera_lens_group()]

    return {
        "animationModel": {"roving": False, "logarithmic": False, "groupedPosition": True},
        "duration": duration_frames,
        "attributes": [camera_group, _environment_group()],
        "cameraExport": {"logarithmic": False, "modelVersion": 2},
    }


def build_orbit_esp(
    target,
    waypoints,
    *,
    name="orbit",
    frame_rate=30,
    width=1920,
    height=1080,
    seconds_per_leg=5,
    close_loop=True,
):
    """Build an .esp project dict that flies through `waypoints`, always facing `target`.

    target: {"lat": ..., "lon": ..., "alt_m": ...} - point the camera looks at.
    waypoints: ordered list of {"lat": ..., "lon": ..., "alt_m": ...} camera positions.

    Every alt_m is meters above sea level, camera positions included -- no height
    here is relative to anything, and Earth Studio clamps a camera that ends up
    below the terrain.
    """
    legs = list(waypoints)
    if close_loop:
        legs = legs + [legs[0]]

    n = len(legs)
    times = [i / (n - 1) for i in range(n)]

    lons = [w["lon"] for w in legs]
    lats = [w["lat"] for w in legs]
    alts = [w["alt_m"] for w in legs]
    headings = _unwrap_headings(
        [bearing_between(w["lat"], w["lon"], target["lat"], target["lon"]) for w in legs]
    )
    tilts = [_tilt_for(w, target) for w in legs]

    pan_range = (
        (min(headings), max(headings))
        if min(headings) != max(headings)
        else _pan_range(headings[0])
    )

    duration_frames = round((n - 1) * seconds_per_leg * frame_rate)
    alts_frac = [_altitude(a) for a in alts]

    camera_position_group = {
        "type": "cameraPositionGroup",
        "inTimeline": True,
        "attributes": [
            _block("longitude", lons[0], LON_RANGE, times=times, values=lons),
            _block("latitude", lats[0], LAT_RANGE, times=times, values=lats),
            _block("altitude", alts_frac[0], times=times, values=alts_frac, altitude=True),
        ],
    }

    camera_target_effect = {
        "type": "cameraTargetEffect",
        "attributes": [
            {"type": "enabled", "value": {"relative": 1}},
            {
                "type": "poi",
                "attributes": [
                    _block("longitudePOI", target["lon"], LON_RANGE),
                    _block("latitudePOI", target["lat"], LAT_RANGE),
                    _block("altitudePOI", _altitude(target["alt_m"]), altitude=True),
                ],
            },
            {"type": "influence", "value": {"relative": 1}},
        ],
    }

    camera_rotation_group = {
        "type": "cameraRotationGroup",
        "inTimeline": True,
        "attributes": [
            _block("rotationX", headings[0], pan_range, times=times, values=headings),
            _block("rotationY", tilts[0], TILT_RANGE, times=times, values=tilts),
            {"type": "rotationZ", "value": {"relative": 0}},
        ],
    }

    scene = _scene(
        duration_frames=duration_frames,
        camera_group_attrs=[
            camera_position_group,
            camera_target_effect,
            camera_rotation_group,
        ],
        camera_group_in_timeline=True,
    )

    return _esp(
        scene,
        name=name,
        frame_rate=frame_rate,
        width=width,
        height=height,
        duration_frames=duration_frames,
    )


def build_still_esp(
    lat, lon, alt_m, *, heading=0.0, tilt=45.0, name="frame", frame_rate=30, width=1920, height=1080
):
    """Build an .esp project dict for a single static frame: one camera pose, no keyframes,
    no target POI (heading/tilt are given directly instead of derived from facing a target).

    lat/lon/alt_m: camera position, alt_m in meters above sea level. heading: compass
    bearing in degrees the camera faces. tilt: degrees from straight down (0 = straight
    down, 90 = horizon).
    """
    pan_range = _pan_range(heading)

    camera_position_group = {
        "type": "cameraPositionGroup",
        "attributes": [
            _block("longitude", lon, LON_RANGE),
            _block("latitude", lat, LAT_RANGE),
            _block("altitude", _altitude(alt_m), altitude=True),
        ],
    }

    camera_rotation_group = {
        "type": "cameraRotationGroup",
        "attributes": [
            _block("rotationX", heading, pan_range),
            _block("rotationY", tilt, TILT_RANGE),
            {"type": "rotationZ", "value": {"relative": 0}},
        ],
    }

    scene = _scene(
        duration_frames=0,
        camera_group_attrs=[camera_position_group, camera_rotation_group],
        camera_group_in_timeline=False,
    )

    return _esp(
        scene, name=name, frame_rate=frame_rate, width=width, height=height, duration_frames=0
    )


def write_esp(esp_dict, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(esp_dict, f, indent=2)


def command(
    lat: Annotated[float, typer.Option(help="Target latitude, in degrees.")],
    lon: Annotated[float, typer.Option(help="Target longitude, in degrees.")],
    ground_alt: Annotated[
        float, typer.Option(help="Target's ground elevation, in meters above sea level.")
    ] = 0.0,
    radius: Annotated[
        float, typer.Option(help="Orbit radius around the target, in meters.")
    ] = 200.0,
    camera_alt: Annotated[
        float,
        typer.Option(
            help=(
                "Camera altitude, in meters above sea level. Absolute, not relative to the "
                "target: Earth Studio clamps a camera below the terrain, so this has to clear "
                "the ground at the orbit radius, not just the target."
            )
        ),
    ] = 300.0,
    waypoints: Annotated[
        int,
        typer.Option(min=2, help="Number of camera waypoints spaced evenly around the orbit."),
    ] = 3,
    seconds_per_leg: Annotated[
        float, typer.Option(help="Flight time between consecutive waypoints, in seconds.")
    ] = 5.0,
    frame_rate: Annotated[int, typer.Option(help="Project frame rate.")] = 30,
    width: Annotated[int, typer.Option(help="Video width in pixels.")] = 1920,
    height: Annotated[int, typer.Option(help="Video height in pixels.")] = 1080,
    name: Annotated[str, typer.Option(help="Project name embedded in the .esp file.")] = "orbit",
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Output .esp file path (default: out/<name>.esp)."),
    ] = None,
) -> None:
    """Generate a Google Earth Studio .esp project that orbits a point of interest."""
    output_path = output or REPO_ROOT / "out" / f"{name}.esp"

    target = {"lat": lat, "lon": lon, "alt_m": ground_alt}

    bearings = [i * 360.0 / waypoints for i in range(waypoints)]
    orbit = []
    for bearing_deg in bearings:
        wp_lat, wp_lon = destination_point(target["lat"], target["lon"], bearing_deg, radius)
        orbit.append({"lat": wp_lat, "lon": wp_lon, "alt_m": camera_alt})

    print("earth build")
    print(
        f"Orbiting ({target['lat']:.5f}, {target['lon']:.5f}) "
        f"at radius {radius:.0f}m, camera altitude {camera_alt:.0f}m "
        f"above sea level (target ground {ground_alt:.0f}m)."
    )
    if camera_alt <= ground_alt:
        print(
            f"Warning: --camera-alt {camera_alt:.0f}m is at or below the "
            f"target's ground elevation ({ground_alt:.0f}m); Earth Studio will clamp the "
            "camera to the terrain."
        )

    print(f"\n{'#':>3} {'Bearing':>8} {'Latitude':>10} {'Longitude':>11} {'Altitude (m ASL)':>17}")
    for i, (bearing_deg, w) in enumerate(zip(bearings, orbit), start=1):
        print(
            f"{i:>3} {bearing_deg:>7.1f}° {w['lat']:>10.5f} {w['lon']:>11.5f} {w['alt_m']:>17.0f}"
        )

    esp = build_orbit_esp(
        target=target,
        waypoints=orbit,
        name=name,
        frame_rate=frame_rate,
        width=width,
        height=height,
        seconds_per_leg=seconds_per_leg,
    )
    write_esp(esp, output_path)

    duration_s = esp["settings"]["duration"] / frame_rate
    print(f"\nWrote {output_path} ({duration_s:.1f}s @ {frame_rate}fps, {width}x{height})")
