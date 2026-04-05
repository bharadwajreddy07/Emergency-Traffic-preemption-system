import argparse
import csv
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass


def ensure_sumo_import():
    if "SUMO_HOME" not in os.environ:
        raise RuntimeError("Please set SUMO_HOME to your SUMO installation path")
    tools = os.path.join(os.environ["SUMO_HOME"], "tools")
    if tools not in sys.path:
        sys.path.append(tools)
    import sumolib  # pylint: disable=import-outside-toplevel

    return sumolib


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_") or "hospital"


def parse_tags(element) -> dict[str, str]:
    tags = {}
    for t in element.findall("tag"):
        k = t.attrib.get("k", "")
        v = t.attrib.get("v", "")
        if k:
            tags[k] = v
    return tags


def is_hospital(tags: dict[str, str]) -> bool:
    return tags.get("amenity", "").lower() == "hospital" or tags.get("healthcare", "").lower() == "hospital"


@dataclass
class HospitalPoint:
    name: str
    lat: float
    lon: float


def extract_hospitals_from_osm(osm_path: str) -> list[HospitalPoint]:
    tree = ET.parse(osm_path)
    root = tree.getroot()

    node_coords: dict[str, tuple[float, float]] = {}
    hospitals: list[HospitalPoint] = []

    for node in root.findall("node"):
        node_id = node.attrib.get("id")
        lat = node.attrib.get("lat")
        lon = node.attrib.get("lon")
        if node_id and lat and lon:
            node_coords[node_id] = (float(lat), float(lon))

        tags = parse_tags(node)
        if is_hospital(tags):
            name = tags.get("name") or tags.get("operator") or f"Hospital_{node_id}"
            hospitals.append(HospitalPoint(name=name, lat=float(lat), lon=float(lon)))

    for way in root.findall("way"):
        tags = parse_tags(way)
        if not is_hospital(tags):
            continue

        refs = [nd.attrib.get("ref") for nd in way.findall("nd") if nd.attrib.get("ref")]
        coords = [node_coords[r] for r in refs if r in node_coords]
        if not coords:
            continue

        avg_lat = sum(c[0] for c in coords) / len(coords)
        avg_lon = sum(c[1] for c in coords) / len(coords)
        way_id = way.attrib.get("id", "")
        name = tags.get("name") or tags.get("operator") or f"HospitalWay_{way_id}"
        hospitals.append(HospitalPoint(name=name, lat=avg_lat, lon=avg_lon))

    dedup = {}
    for h in hospitals:
        key = (round(h.lat, 6), round(h.lon, 6), h.name.strip().lower())
        dedup[key] = h

    return list(dedup.values())


def nearest_drive_edge(sumolib, net, lon: float, lat: float, radius: float = 250.0):
    x, y = net.convertLonLat2XY(lon, lat)
    neighbors = net.getNeighboringEdges(x, y, radius)
    if not neighbors:
        return None

    filtered = []
    for edge, dist in neighbors:
        edge_id = edge.getID()
        if edge_id.startswith(":"):
            continue
        if edge.getFunction() and edge.getFunction() != "normal":
            continue
        # Keep edges suitable for rerouting road ambulances.
        if not (edge.allows("emergency") or edge.allows("passenger")):
            continue
        filtered.append((edge, dist))

    if not filtered:
        filtered = neighbors

    filtered.sort(key=lambda p: p[1])
    return filtered[0][0]


def build_records(sumolib, net_path: str, hospitals: list[HospitalPoint], default_capacity: int):
    net = sumolib.net.readNet(net_path)
    rows = []
    edge_map: dict[str, str] = {}

    used_ids: dict[str, int] = {}

    for h in hospitals:
        base = slugify(h.name)
        idx = used_ids.get(base, 0)
        used_ids[base] = idx + 1
        hospital_id = base if idx == 0 else f"{base}_{idx+1}"

        edge = nearest_drive_edge(sumolib, net, h.lon, h.lat)
        if edge is None:
            continue

        rows.append(
            {
                "hospital_id": hospital_id,
                "name": h.name,
                "lat": f"{h.lat:.6f}",
                "lon": f"{h.lon:.6f}",
                "capacity_available": str(default_capacity),
                "supports_trauma": "1",
                "supports_cardiac": "1",
                "supports_stroke": "1",
                "endpoint": "",
            }
        )
        edge_map[hospital_id] = edge.getID()

    return rows, edge_map


def write_csv(path: str, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "hospital_id",
        "name",
        "lat",
        "lon",
        "capacity_available",
        "supports_trauma",
        "supports_cardiac",
        "supports_stroke",
        "endpoint",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def update_config_hospital_map(config_path: str, edge_map: dict[str, str]) -> None:
    if not os.path.exists(config_path):
        cfg = {}
    else:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    cfg["hospital_edge_map"] = edge_map

    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate full Hyderabad hospital registry from OSM and SUMO net")
    parser.add_argument("--osm", required=True, help="Hyderabad OSM XML path")
    parser.add_argument("--net", required=True, help="SUMO net.xml path")
    parser.add_argument("--out-csv", default="hyderabad_hospitals.csv")
    parser.add_argument("--out-edge-map", default="config/hospital_edge_map.generated.json")
    parser.add_argument("--update-config", default="config/hyderabad_example.json")
    parser.add_argument("--default-capacity", type=int, default=20)
    args = parser.parse_args()

    sumolib = ensure_sumo_import()

    hospitals = extract_hospitals_from_osm(args.osm)
    rows, edge_map = build_records(sumolib, args.net, hospitals, args.default_capacity)

    if not rows:
        raise RuntimeError("No hospitals mapped to drivable SUMO edges. Check OSM/net inputs.")

    write_csv(args.out_csv, rows)

    os.makedirs(os.path.dirname(args.out_edge_map) or ".", exist_ok=True)
    with open(args.out_edge_map, "w", encoding="utf-8") as f:
        json.dump(edge_map, f, indent=2)

    update_config_hospital_map(args.update_config, edge_map)

    print(f"Hospitals discovered in OSM: {len(hospitals)}")
    print(f"Hospitals mapped to SUMO edges: {len(rows)}")
    print(f"Wrote hospital CSV: {args.out_csv}")
    print(f"Wrote edge map JSON: {args.out_edge_map}")
    print(f"Updated config: {args.update_config}")


if __name__ == "__main__":
    main()
