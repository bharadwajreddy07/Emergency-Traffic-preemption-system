import argparse
import xml.etree.ElementTree as ET


def gather_candidate_routes(route_file: str) -> list[str]:
    tree = ET.parse(route_file)
    root = tree.getroot()
    candidates: list[str] = []

    route_map = {}
    for route in root.findall("route"):
        route_id = route.attrib.get("id", "")
        edges = route.attrib.get("edges", "").strip()
        if route_id and edges:
            route_map[route_id] = edges
            candidates.append(edges)

    for vehicle in root.findall("vehicle"):
        inline_route = vehicle.find("route")
        if inline_route is not None:
            edges = inline_route.attrib.get("edges", "").strip()
            if edges:
                candidates.append(edges)
                continue

        route_ref = vehicle.attrib.get("route", "")
        if route_ref in route_map:
            candidates.append(route_map[route_ref])

    # Preserve order, remove duplicates.
    seen = set()
    unique = []
    for edges in candidates:
        if edges not in seen:
            seen.add(edges)
            unique.append(edges)
    return unique


def tls_controlled_edges(net_file: str) -> set[str]:
    tree = ET.parse(net_file)
    root = tree.getroot()
    controlled: set[str] = set()

    for conn in root.findall("connection"):
        if "tl" not in conn.attrib:
            continue
        from_edge = conn.attrib.get("from", "").strip()
        if from_edge:
            controlled.add(from_edge)

    return controlled


def filter_routes_with_tls(routes: list[str], controlled_edges: set[str]) -> list[str]:
    filtered: list[str] = []
    for edges in routes:
        parts = edges.split()
        if any(edge in controlled_edges for edge in parts):
            filtered.append(edges)
    return filtered


def write_ambulance_routes(out_file: str, routes: list[str], count: int, depart_start: float, depart_gap: float) -> None:
    if count < 1:
        raise ValueError("Ambulance count must be at least 1")

    lines = [
        "<routes>",
        "  <vType id=\"emergency\" vClass=\"emergency\" accel=\"3.2\" decel=\"5.5\" sigma=\"0.1\" length=\"5.0\" minGap=\"1.0\" maxSpeed=\"31.0\" speedFactor=\"1.35\" lcAssertive=\"1.3\" lcCooperative=\"0.3\" guiShape=\"emergency\" color=\"1,0,0\"/>",
    ]

    for i in range(count):
        edges = routes[i % len(routes)]
        depart = depart_start + i * depart_gap
        lines.extend(
            [
                f"  <vehicle id=\"ambulance_{i + 1}\" type=\"emergency\" depart=\"{depart}\" departLane=\"best\" departSpeed=\"max\">",
                "    <param key=\"has.bluelight.device\" value=\"true\"/>",
                "    <param key=\"device.bluelight.reactiondist\" value=\"75\"/>",
                f"    <route edges=\"{edges}\"/>",
                "  </vehicle>",
            ]
        )

    lines.append("</routes>")

    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create multiple emergency ambulance routes from existing valid routes")
    parser.add_argument("--base-route", default="hyderabad_car.rou.xml")
    parser.add_argument("--out", default="emergency_vehicle.rou.xml")
    parser.add_argument("--net", default="hyderabad.net.xml", help="SUMO net file used to detect TLS-controlled edges")
    parser.add_argument(
        "--require-tls-route",
        action="store_true",
        help="Only use routes that include at least one traffic-light-controlled edge",
    )
    parser.add_argument("--count", type=int, default=50, help="Ambulance count (>= 1)")
    parser.add_argument("--depart-start", type=float, default=10.0)
    parser.add_argument("--depart-gap", type=float, default=25.0)
    args = parser.parse_args()

    routes = gather_candidate_routes(args.base_route)
    if args.require_tls_route:
        controlled_edges = tls_controlled_edges(args.net)
        tls_routes = filter_routes_with_tls(routes, controlled_edges)
        if tls_routes:
            routes = tls_routes
            print(f"Filtered to {len(routes)} TLS-crossing candidate routes")
        else:
            print("No TLS-crossing candidate routes found; using all candidate routes")
    if not routes:
        raise RuntimeError("No valid routes found in base route file")

    write_ambulance_routes(args.out, routes, args.count, args.depart_start, args.depart_gap)
    print(f"Wrote {args.out} with {args.count} ambulances")


if __name__ == "__main__":
    main()
