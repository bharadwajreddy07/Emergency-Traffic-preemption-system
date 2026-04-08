import argparse
import json
import xml.etree.ElementTree as ET


def load_hospital_edges(config_file: str) -> list[tuple[str, str]]:
    with open(config_file, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    mapping = cfg.get("hospital_edge_map", {})
    items = []
    for hospital_id, edge_id in mapping.items():
        if edge_id and not str(edge_id).startswith(":"):
            items.append((str(hospital_id), str(edge_id)))
    return items


def build_successor_map(net_file: str) -> dict[str, list[str]]:
    tree = ET.parse(net_file)
    root = tree.getroot()
    succ: dict[str, list[str]] = {}
    for conn in root.findall("connection"):
        fr = str(conn.attrib.get("from", "")).strip()
        to = str(conn.attrib.get("to", "")).strip()
        if not fr or not to or to.startswith(":"):
            continue
        succ.setdefault(fr, [])
        if to not in succ[fr]:
            succ[fr].append(to)
    return succ


def write_routes(out_file: str, hospitals: list[tuple[str, str]], succ_map: dict[str, list[str]], per_hospital: int) -> int:
    lines = [
        "<routes>",
        "  <vType id=\"emergency\" vClass=\"emergency\" accel=\"3.2\" decel=\"5.5\" sigma=\"0.1\" length=\"5.0\" minGap=\"1.0\" maxSpeed=\"31.0\" speedFactor=\"1.2\" lcAssertive=\"1.2\" lcCooperative=\"0.4\" guiShape=\"emergency\" color=\"1,0,0\"/>",
    ]

    idx = 0
    for hospital_id, edge_id in hospitals:
        neighbors = succ_map.get(edge_id, [])
        if neighbors:
            route_edges = f"{edge_id} {neighbors[0]}"
        else:
            route_edges = edge_id

        for slot in range(per_hospital):
            idx += 1
            depart = idx
            stand_pos = 3 + (slot * 2)
            lines.extend(
                [
                    f"  <vehicle id=\"ambulance_{idx}\" type=\"emergency\" depart=\"{depart}\" departLane=\"free\" departPos=\"{stand_pos}\" departSpeed=\"0\">",
                    "    <param key=\"has.bluelight.device\" value=\"true\"/>",
                    "    <param key=\"device.bluelight.reactiondist\" value=\"75\"/>",
                    f"    <param key=\"home.hospital\" value=\"{hospital_id}\"/>",
                    f"    <route edges=\"{route_edges}\"/>",
                    f"    <stop edge=\"{edge_id}\" endPos=\"{stand_pos}\" duration=\"100000\"/>",
                    "  </vehicle>",
                ]
            )

    lines.append("</routes>")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return idx


def main() -> None:
    parser = argparse.ArgumentParser(description="Create ambulances initially stationed at hospitals")
    parser.add_argument("--config", default="config/hyderabad_example.json")
    parser.add_argument("--net", default="hyderabad.net.xml")
    parser.add_argument("--out", default="emergency_vehicle.rou.xml")
    parser.add_argument("--per-hospital", type=int, default=3)
    args = parser.parse_args()

    hospitals = load_hospital_edges(args.config)
    if not hospitals:
        raise RuntimeError("No hospital_edge_map found in config")

    succ_map = build_successor_map(args.net)
    count = write_routes(args.out, hospitals, succ_map, max(1, args.per_hospital))
    print(f"Wrote {args.out} with {count} ambulances ({args.per_hospital} per hospital)")


if __name__ == "__main__":
    main()
