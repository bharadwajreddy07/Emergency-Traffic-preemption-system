import argparse
import json
import xml.etree.ElementTree as ET

from route_planner import StaticGraphRouter


def parse_ambulance_start_edges(route_file: str) -> list[str]:
    tree = ET.parse(route_file)
    root = tree.getroot()

    starts: list[str] = []
    route_map: dict[str, str] = {}

    for route in root.findall("route"):
        route_id = route.attrib.get("id", "")
        edges = route.attrib.get("edges", "").strip()
        if route_id and edges:
            route_map[route_id] = edges

    for vehicle in root.findall("vehicle"):
        vehicle_id = vehicle.attrib.get("id", "")
        vehicle_type = vehicle.attrib.get("type", "")
        if not (vehicle_id.startswith("ambulance_") or vehicle_type == "emergency"):
            continue

        edges = ""
        inline_route = vehicle.find("route")
        if inline_route is not None:
            edges = inline_route.attrib.get("edges", "").strip()
        else:
            route_ref = vehicle.attrib.get("route", "")
            edges = route_map.get(route_ref, "")

        if edges:
            first = edges.split()[0]
            if first and not first.startswith(":"):
                starts.append(first)

    seen = set()
    unique = []
    for edge in starts:
        if edge in seen:
            continue
        seen.add(edge)
        unique.append(edge)
    return unique


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate dedicated shortest paths from ambulance starts to hospitals")
    parser.add_argument("--net", default="hyderabad.net.xml")
    parser.add_argument("--ambulance-routes", default="emergency_vehicle.rou.xml")
    parser.add_argument("--config", default="config/hyderabad_example.json")
    parser.add_argument("--out", default="config/hospital_priority_paths.generated.json")
    parser.add_argument("--algorithm", default="dijkstra", choices=["dijkstra", "astar"])
    args = parser.parse_args()

    router = StaticGraphRouter.from_net_file(args.net)

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    hospital_edge_map = cfg.get("hospital_edge_map", {})

    ambulance_starts = parse_ambulance_start_edges(args.ambulance_routes)

    by_hospital: dict[str, dict] = {}
    for hospital_id, target_edge in hospital_edge_map.items():
        best = {
            "start_edge": "",
            "eta_seconds": float("inf"),
            "route_edges": [],
        }
        for start_edge in ambulance_starts:
            route_edges, eta_s = router.shortest_path(start_edge, target_edge, args.algorithm)
            if route_edges and eta_s < best["eta_seconds"]:
                best = {
                    "start_edge": start_edge,
                    "eta_seconds": round(float(eta_s), 2),
                    "route_edges": route_edges,
                }
        if best["route_edges"]:
            by_hospital[hospital_id] = best

    out_doc = {
        "algorithm": args.algorithm,
        "ambulance_start_edges": ambulance_starts,
        "hospital_priority_paths": by_hospital,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out_doc, f, indent=2)

    print(f"Wrote dedicated hospital priority paths: {args.out}")


if __name__ == "__main__":
    main()
