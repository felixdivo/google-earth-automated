"""Conversion tool: coordinates + view direction/height -> Google Earth Studio .esp project.

The .esp format is not officially documented by Google. This module's structure
and value-encoding formulas were cross-referenced from two independent community
implementations and a real Earth-Studio-exported template, so it should open
correctly but may need small manual tweaks in-app.

Also doubles as a CLI (`esp-builder`) for generating a circular orbit .esp
around a single point of interest.
"""

import argparse
import json
import math
import sys
from pathlib import Path

EARTH_RADIUS_M = 6371000.0

LON_RANGE = (-180.0, 180.0)
LAT_RANGE = (-90.0, 90.0)
ALT_RANGE = (-500.0, 65117481.0)
TILT_RANGE = (0.0, 180.0)


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


def _keyframed_block(attr_type, times, values, value_range=None, logarithmic=None):
    value = {"relative": values[0] if not value_range else _relative(values[0], value_range)}
    if value_range is not None:
        value["minValueRange"], value["maxValueRange"] = value_range
    if logarithmic is not None:
        value["logarithmic"] = logarithmic

    keyframes = [
        {
            "time": t,
            "value": v if not value_range else _relative(v, value_range),
        }
        for t, v in zip(times, values)
    ]

    return {
        "type": attr_type,
        "value": value,
        "keyframes": keyframes,
        "inTimeline": True,
    }


def _static_poi_block(attr_type, value, value_range=None, logarithmic=None):
    block_value = {"relative": value if not value_range else _relative(value, value_range)}
    if value_range is not None:
        block_value["minValueRange"], block_value["maxValueRange"] = value_range
    if logarithmic is not None:
        block_value["logarithmic"] = logarithmic
    return {"type": attr_type, "value": block_value}


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
        else (headings[0] - 0.5, headings[0] + 0.5)
    )

    duration_frames = round((n - 1) * seconds_per_leg * frame_rate)

    camera_position_group = {
        "type": "cameraPositionGroup",
        "inTimeline": True,
        "attributes": [
            _keyframed_block("longitude", times, lons, LON_RANGE),
            _keyframed_block("latitude", times, lats, LAT_RANGE),
            _keyframed_block("altitude", times, alts, ALT_RANGE, logarithmic=False),
        ],
    }

    camera_target_effect = {
        "type": "cameraTargetEffect",
        "attributes": [
            {"type": "enabled", "value": {"relative": 1}},
            {
                "type": "poi",
                "attributes": [
                    _static_poi_block("longitudePOI", target["lon"], LON_RANGE),
                    _static_poi_block("latitudePOI", target["lat"], LAT_RANGE),
                    _static_poi_block("altitudePOI", target["alt_m"], ALT_RANGE, logarithmic=False),
                ],
            },
            {"type": "influence", "value": {"relative": 1}},
        ],
    }

    camera_rotation_group = {
        "type": "cameraRotationGroup",
        "inTimeline": True,
        "attributes": [
            _keyframed_block("rotationX", times, headings, pan_range),
            _keyframed_block("rotationY", times, tilts, TILT_RANGE),
            {"type": "rotationZ", "value": {"relative": 0}},
        ],
    }

    camera_lens_group = {
        "type": "cameraLensGroup",
        "attributes": [
            {"type": "fov", "value": {}},
            {"type": "exposure", "value": {}},
            {"type": "aperture", "value": {}},
            {"type": "minFocusLength", "value": {}},
        ],
    }

    environment_group = {
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

    scene = {
        "animationModel": {"roving": False, "logarithmic": False, "groupedPosition": True},
        "duration": duration_frames,
        "attributes": [
            {
                "type": "cameraGroup",
                "inTimeline": True,
                "attributes": [
                    camera_position_group,
                    camera_target_effect,
                    camera_rotation_group,
                    camera_lens_group,
                ],
            },
            environment_group,
        ],
        "cameraExport": {"logarithmic": False, "modelVersion": 2},
    }

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


def write_esp(esp_dict, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(esp_dict, f, indent=2)


def _build_cli_parser():
    parser = argparse.ArgumentParser(
        prog="esp-builder",
        description="Generate a Google Earth Studio .esp project that orbits a point of interest.",
    )
    parser.add_argument("--lat", type=float, required=True, help="Target latitude, in degrees.")
    parser.add_argument("--lon", type=float, required=True, help="Target longitude, in degrees.")
    parser.add_argument(
        "--ground-alt",
        type=float,
        default=0.0,
        help="Target's ground elevation, in meters above sea level (default: 0).",
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=200.0,
        help="Orbit radius around the target, in meters (default: 200).",
    )
    parser.add_argument(
        "--camera-height",
        type=float,
        default=30.0,
        help="Camera height above the target's ground elevation, in meters (default: 30).",
    )
    parser.add_argument(
        "--waypoints",
        type=int,
        default=3,
        help="Number of camera waypoints spaced evenly around the orbit (default: 3).",
    )
    parser.add_argument(
        "--start-bearing",
        type=float,
        default=0.0,
        help="Compass bearing of the first waypoint, in degrees (default: 0).",
    )
    parser.add_argument(
        "--seconds-per-leg",
        type=float,
        default=5.0,
        help="Flight time between consecutive waypoints, in seconds (default: 5).",
    )
    parser.add_argument(
        "--open-loop",
        action="store_true",
        help="Don't fly back to the first waypoint at the end.",
    )
    parser.add_argument(
        "--frame-rate", type=int, default=30, help="Project frame rate (default: 30)."
    )
    parser.add_argument(
        "--width", type=int, default=1920, help="Video width in pixels (default: 1920)."
    )
    parser.add_argument(
        "--height", type=int, default=1080, help="Video height in pixels (default: 1080)."
    )
    parser.add_argument(
        "--name", default="orbit", help="Project name embedded in the .esp file (default: orbit)."
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output .esp file path (default: out/<name>.esp).",
    )
    return parser


def main(argv=None):
    from rich.console import Console
    from rich.table import Table

    console = Console()
    args = _build_cli_parser().parse_args(argv)

    if args.waypoints < 2:
        console.print("[bold red]Error:[/] --waypoints must be at least 2.")
        return 1

    output_path = args.output or f"out/{args.name}.esp"

    target = {"lat": args.lat, "lon": args.lon, "alt_m": args.ground_alt}

    waypoints = []
    for i in range(args.waypoints):
        bearing_deg = args.start_bearing + i * 360.0 / args.waypoints
        lat, lon = destination_point(target["lat"], target["lon"], bearing_deg, args.radius)
        waypoints.append({"lat": lat, "lon": lon, "alt_m": target["alt_m"] + args.camera_height})

    console.rule("[bold cyan]esp-builder[/]")
    console.print(
        f"Orbiting [bold]({target['lat']:.5f}, {target['lon']:.5f})[/] "
        f"at radius [bold]{args.radius:.0f}m[/], camera height [bold]{args.camera_height:.0f}m[/]."
    )

    table = Table(title="Waypoints", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Bearing", justify="right")
    table.add_column("Latitude", justify="right")
    table.add_column("Longitude", justify="right")
    table.add_column("Altitude (m)", justify="right")
    for i, w in enumerate(waypoints):
        bearing_deg = args.start_bearing + i * 360.0 / args.waypoints
        table.add_row(
            str(i + 1),
            f"{bearing_deg:.1f}°",
            f"{w['lat']:.5f}",
            f"{w['lon']:.5f}",
            f"{w['alt_m']:.0f}",
        )
    console.print(table)

    esp = build_orbit_esp(
        target=target,
        waypoints=waypoints,
        name=args.name,
        frame_rate=args.frame_rate,
        width=args.width,
        height=args.height,
        seconds_per_leg=args.seconds_per_leg,
        close_loop=not args.open_loop,
    )
    write_esp(esp, output_path)

    duration_s = esp["settings"]["duration"] / args.frame_rate
    console.print(
        f"\n[bold green]✓[/] Wrote [bold]{output_path}[/] "
        f"({duration_s:.1f}s @ {args.frame_rate}fps, {args.width}x{args.height})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
