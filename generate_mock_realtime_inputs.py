import argparse
import json
import os
import random
import time
import xml.etree.ElementTree as ET


def load_edge_ids(net_file: str) -> list[str]:
    if not os.path.exists(net_file):
        return []

    try:
        tree = ET.parse(net_file)
    except ET.ParseError:
        return []

    root = tree.getroot()
    edge_ids: list[str] = []
    for edge in root.findall("edge"):
        edge_id = edge.attrib.get("id", "")
        if not edge_id or edge_id.startswith(":"):
            continue
        edge_ids.append(edge_id)
    return edge_ids


def write_live_traffic(path: str, edge_ids: list[str], edge_count: int, incident_rate: float) -> dict:
    ts = time.time()
    sample = random.sample(edge_ids, k=min(max(1, edge_count), len(edge_ids))) if edge_ids else []

    rows = []
    for edge_id in sample:
        speed = random.uniform(8.0, 42.0)
        occupancy = random.uniform(0.05, 0.95)
        confidence = random.uniform(0.65, 0.99)
        incident = random.random() < incident_rate
        if incident:
            speed = min(speed, random.uniform(3.0, 12.0))
            occupancy = max(occupancy, random.uniform(0.75, 0.99))

        rows.append(
            {
                "edge_id": edge_id,
                "speed_kmh": round(speed, 2),
                "occupancy": round(occupancy, 3),
                "confidence": round(confidence, 3),
                "incident": incident,
                "timestamp": round(ts, 3),
            }
        )

    payload = {
        "timestamp": round(ts, 3),
        "edges": rows,
    }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    return payload


def append_lora_events(
    path: str,
    ambulance_count: int,
    center_lat: float,
    center_lon: float,
    spread_deg: float,
) -> list[dict]:
    ts = time.time()
    events: list[dict] = []
    for idx in range(ambulance_count):
        ambulance_id = f"ambulance_{idx + 1}"
        lat = center_lat + random.uniform(-spread_deg, spread_deg)
        lon = center_lon + random.uniform(-spread_deg, spread_deg)
        events.append(
            {
                "timestamp": round(ts, 3),
                "ambulance_id": ambulance_id,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "emergency": True,
                "wireless": True,
                "confidence": round(random.uniform(0.75, 0.99), 3),
            }
        )

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in events:
            f.write(json.dumps(row) + "\n")

    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate mock live traffic and LoRa events for demo")
    parser.add_argument("--net-file", default="hyderabad.net.xml")
    parser.add_argument("--live-traffic-file", default="out/live_traffic.json")
    parser.add_argument("--lora-events-file", default="out/lora_events.jsonl")
    parser.add_argument("--edge-count", type=int, default=120)
    parser.add_argument("--ambulance-count", type=int, default=2)
    parser.add_argument("--incident-rate", type=float, default=0.06)
    parser.add_argument("--center-lat", type=float, default=17.4435)
    parser.add_argument("--center-lon", type=float, default=78.3850)
    parser.add_argument("--spread-deg", type=float, default=0.02)
    parser.add_argument("--interval-s", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=0, help="0 means run forever")
    parser.add_argument("--truncate-lora", action="store_true", help="Clear existing lora_events file before generation")
    args = parser.parse_args()

    edge_ids = load_edge_ids(args.net_file)
    if not edge_ids:
        print(f"[MOCK] No edges parsed from {args.net_file}. live_traffic will be empty.")

    if args.truncate_lora:
        os.makedirs(os.path.dirname(args.lora_events_file) or ".", exist_ok=True)
        with open(args.lora_events_file, "w", encoding="utf-8"):
            pass

    loops = 0
    while True:
        loops += 1
        live_payload = write_live_traffic(
            path=args.live_traffic_file,
            edge_ids=edge_ids,
            edge_count=max(1, int(args.edge_count)),
            incident_rate=max(0.0, min(1.0, float(args.incident_rate))),
        )
        lora_events = append_lora_events(
            path=args.lora_events_file,
            ambulance_count=max(1, int(args.ambulance_count)),
            center_lat=float(args.center_lat),
            center_lon=float(args.center_lon),
            spread_deg=max(0.0001, float(args.spread_deg)),
        )

        print(
            "[MOCK] tick="
            f"{loops} live_edges={len(live_payload.get('edges', []))} "
            f"lora_events={len(lora_events)}"
        )

        if args.iterations > 0 and loops >= args.iterations:
            break

        time.sleep(max(0.1, float(args.interval_s)))


if __name__ == "__main__":
    main()
