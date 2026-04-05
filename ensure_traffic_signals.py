import argparse
import os
import subprocess
import sys
import xml.etree.ElementTree as ET


def count_tls(net_file: str) -> int:
    tree = ET.parse(net_file)
    root = tree.getroot()
    return len(root.findall("tlLogic"))


def ensure_netconvert_available() -> str:
    exe = "netconvert.exe" if os.name == "nt" else "netconvert"
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, exe)
        if os.path.isfile(candidate):
            return candidate

    sumo_home = os.environ.get("SUMO_HOME", "")
    if sumo_home:
        candidate = os.path.join(sumo_home, "bin", exe)
        if os.path.isfile(candidate):
            return candidate

    raise RuntimeError("netconvert executable not found in PATH or SUMO_HOME/bin")


def rebuild_with_tls(osm_file: str, net_file: str) -> None:
    netconvert = ensure_netconvert_available()
    cmd = [
        netconvert,
        "--osm-files",
        osm_file,
        "--output-file",
        net_file,
        "--tls.guess",
        "true",
        "--tls.default-type",
        "actuated",
        "--junctions.join",
        "true",
        "--geometry.remove",
        "true",
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ensure SUMO net has traffic signals; rebuild from OSM if missing")
    parser.add_argument("--net", default="hyderabad.net.xml")
    parser.add_argument("--osm", default="map.osm")
    parser.add_argument("--require-min", type=int, default=1)
    args = parser.parse_args()

    if not os.path.exists(args.net):
        raise RuntimeError(f"Net file not found: {args.net}")

    before = count_tls(args.net)
    print(f"[TLS] tlLogic count before check: {before}")

    if before >= args.require_min:
        print("[TLS] Traffic signals already available; no rebuild needed.")
        return

    if not os.path.exists(args.osm):
        raise RuntimeError(
            f"Traffic signals missing and OSM source not found for rebuild: {args.osm}"
        )

    print("[TLS] Rebuilding SUMO net with guessed traffic signals from OSM...")
    rebuild_with_tls(args.osm, args.net)

    after = count_tls(args.net)
    print(f"[TLS] tlLogic count after rebuild: {after}")
    if after < args.require_min:
        raise RuntimeError("Failed to generate required traffic signals in net file")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        print(f"[TLS][ERROR] {exc}", file=sys.stderr)
        raise
