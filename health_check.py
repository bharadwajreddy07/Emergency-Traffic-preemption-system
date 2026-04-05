import argparse
import csv
import json
import os
import sys
import xml.etree.ElementTree as ET


def ensure_sumo_import():
    if "SUMO_HOME" not in os.environ:
        raise RuntimeError("SUMO_HOME is not set")
    tools = os.path.join(os.environ["SUMO_HOME"], "tools")
    if tools not in sys.path:
        sys.path.append(tools)
    import sumolib  # pylint: disable=import-outside-toplevel

    return sumolib


def parse_sumocfg(sumocfg_path: str) -> tuple[str, list[str]]:
    tree = ET.parse(sumocfg_path)
    root = tree.getroot()

    net_file_node = root.find("./input/net-file")
    route_files_node = root.find("./input/route-files")

    if net_file_node is None or "value" not in net_file_node.attrib:
        raise RuntimeError("sumocfg missing input/net-file")
    if route_files_node is None or "value" not in route_files_node.attrib:
        raise RuntimeError("sumocfg missing input/route-files")

    base_dir = os.path.dirname(sumocfg_path)
    net_file = os.path.join(base_dir, net_file_node.attrib["value"])
    route_files = [os.path.join(base_dir, p.strip()) for p in route_files_node.attrib["value"].split(",") if p.strip()]
    return net_file, route_files


def parse_ambulance_start_edges(route_files: list[str]) -> list[str]:
    starts: list[str] = []
    for route_file in route_files:
        if not os.path.exists(route_file):
            continue
        tree = ET.parse(route_file)
        root = tree.getroot()

        route_map = {
            route.attrib.get("id", ""): route.attrib.get("edges", "").strip()
            for route in root.findall("route")
        }

        for vehicle in root.findall("vehicle"):
            vehicle_id = vehicle.attrib.get("id", "")
            vehicle_type = vehicle.attrib.get("type", "")
            if not (vehicle_id.startswith("ambulance_") or vehicle_type == "emergency"):
                continue

            edges = ""
            inline_route = vehicle.find("route")
            if inline_route is not None:
                edges = inline_route.attrib.get("edges", "").strip()
            elif vehicle.attrib.get("route", "") in route_map:
                edges = route_map[vehicle.attrib["route"]]

            if edges:
                first_edge = edges.split()[0]
                starts.append(first_edge)

    # unique order
    seen = set()
    out = []
    for e in starts:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-run health check for hospital-edge reachability")
    parser.add_argument("--sumocfg", default="hyderabad.sumocfg")
    parser.add_argument("--hospitals-csv", default="hyderabad_hospitals.csv")
    parser.add_argument("--config", default="config/hyderabad_example.json")
    parser.add_argument("--min-reachability-ratio", type=float, default=0.9)
    args = parser.parse_args()

    sumolib = ensure_sumo_import()
    net_file, route_files = parse_sumocfg(args.sumocfg)

    if not os.path.exists(net_file):
        raise RuntimeError(f"Net file not found: {net_file}")

    net = sumolib.net.readNet(net_file)

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    hospital_edge_map = cfg.get("hospital_edge_map", {})

    hospitals = []
    with open(args.hospitals_csv, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hospitals.append(row)

    ambulance_starts = parse_ambulance_start_edges(route_files)
    if not ambulance_starts:
        raise RuntimeError("No ambulance start edges found in route files")

    missing_edges = []
    disallowed_edges = []
    unreachable_hospitals = []

    checked = 0
    reachable = 0

    for h in hospitals:
        hospital_id = h.get("hospital_id", "")
        if hospital_id not in hospital_edge_map:
            missing_edges.append(hospital_id)
            continue

        target_edge_id = hospital_edge_map[hospital_id]
        try:
            target_edge = net.getEdge(target_edge_id)
        except Exception:
            target_edge = None
        if target_edge is None:
            missing_edges.append(hospital_id)
            continue

        if not (target_edge.allows("emergency") or target_edge.allows("passenger")):
            disallowed_edges.append(hospital_id)
            continue

        checked += 1
        is_reachable = False
        for start_edge_id in ambulance_starts:
            try:
                start_edge = net.getEdge(start_edge_id)
            except Exception:
                continue
            path = net.getShortestPath(start_edge, target_edge, vClass="emergency")
            if path and path[0]:
                is_reachable = True
                break

        if is_reachable:
            reachable += 1
        else:
            unreachable_hospitals.append(hospital_id)

    ratio = (reachable / checked) if checked else 0.0

    print(f"[HEALTH] Hospitals in CSV: {len(hospitals)}")
    print(f"[HEALTH] Hospitals with mapped edges checked: {checked}")
    print(f"[HEALTH] Reachable hospitals: {reachable}")
    print(f"[HEALTH] Reachability ratio: {ratio:.2f}")

    if missing_edges:
        print(f"[HEALTH] Missing edge mapping for: {missing_edges}")
    if disallowed_edges:
        print(f"[HEALTH] Non-drivable hospital edges: {disallowed_edges}")
    if unreachable_hospitals:
        print(f"[HEALTH] Unreachable hospitals: {unreachable_hospitals}")

    if ratio < args.min_reachability_ratio or missing_edges or disallowed_edges:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
