import argparse
import csv
import json
import math
import os
import sys
from xml.sax.saxutils import escape


def ensure_sumo_import():
    if "SUMO_HOME" not in os.environ:
        raise RuntimeError("SUMO_HOME is not set")
    tools = os.path.join(os.environ["SUMO_HOME"], "tools")
    if tools not in sys.path:
        sys.path.append(tools)
    import sumolib  # pylint: disable=import-outside-toplevel

    return sumolib


def _polygon_circle(cx: float, cy: float, radius: float, segments: int = 20) -> str:
    points = []
    for i in range(segments):
        angle = 2.0 * 3.141592653589793 * i / segments
        px = cx + radius * math.cos(angle)
        py = cy + radius * math.sin(angle)
        points.append(f"{px:.2f},{py:.2f}")
    return " ".join(points)


def _rectangle_shape(cx: float, cy: float, width: float, height: float) -> str:
    hw = width / 2.0
    hh = height / 2.0
    return " ".join(
        [
            f"{cx - hw:.2f},{cy - hh:.2f}",
            f"{cx + hw:.2f},{cy - hh:.2f}",
            f"{cx + hw:.2f},{cy + hh:.2f}",
            f"{cx - hw:.2f},{cy + hh:.2f}",
        ]
    )


def add_hospital_symbol(lines: list[str], prefix: str, x: float, y: float, size: float = 9.0) -> None:
    # Draw a map-style hospital icon: white outer ring, red center, and white cross.
    outer = _polygon_circle(x, y, radius=size, segments=24)
    inner = _polygon_circle(x, y, radius=size * 0.78, segments=24)
    lines.append(
        f'  <poly id="{prefix}_ring" type="hospital_ring" color="1,1,1" fill="1" layer="95" shape="{outer}"/>'
    )
    lines.append(
        f'  <poly id="{prefix}_core" type="hospital_core" color="0.83,0.09,0.12" fill="1" layer="96" shape="{inner}"/>'
    )

    cross_h = _rectangle_shape(x, y, width=size * 1.0, height=size * 0.28)
    cross_v = _rectangle_shape(x, y, width=size * 0.28, height=size * 1.0)

    lines.append(
        f'  <poly id="{prefix}_cross_h" type="hospital_cross" color="1,1,1" fill="1" layer="97" shape="{cross_h}"/>'
    )
    lines.append(
        f'  <poly id="{prefix}_cross_v" type="hospital_cross" color="1,1,1" fill="1" layer="97" shape="{cross_v}"/>'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate hospital map-style symbols for SUMO GUI")
    parser.add_argument("--net", default="hyderabad.net.xml")
    parser.add_argument("--hospitals-csv", default="hyderabad_hospitals.csv")
    parser.add_argument("--out", default="hospital_markers.add.xml")
    parser.add_argument("--with-labels", action="store_true")
    parser.add_argument("--symbol-size", type=float, default=9.0, help="Hospital marker radius in SUMO map units")
    args = parser.parse_args()

    sumolib = ensure_sumo_import()
    net = sumolib.net.readNet(args.net)

    rows = []
    with open(args.hospitals_csv, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    lines = ["<additional>"]

    for row in rows:
        hospital_id = row["hospital_id"]
        name = row.get("name", hospital_id)
        lat = float(row["lat"])
        lon = float(row["lon"])
        x, y = net.convertLonLat2XY(lon, lat)

        add_hospital_symbol(lines, hospital_id, x, y, size=float(args.symbol_size))
        # POI dot under plus for easy toggling in GUI layers.
        lines.append(
            f'  <poi id="{hospital_id}_poi" x="{x:.2f}" y="{y:.2f}" color="1,1,1" layer="96" width="3" height="3" type="hospital"/>'
        )
        if args.with_labels:
            safe_name = escape(name).replace('"', "'")
            lines.append(
                f'  <poi id="{hospital_id}_label" x="{x + 8.0:.2f}" y="{y + 8.0:.2f}" color="1,1,1" layer="97" type="{safe_name}"/>'
            )

    lines.append("</additional>")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Wrote hospital marker file: {args.out} ({len(rows)} hospitals)")


if __name__ == "__main__":
    main()
