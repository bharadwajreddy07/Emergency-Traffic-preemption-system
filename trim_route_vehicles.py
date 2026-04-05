import argparse
import xml.etree.ElementTree as ET


def trim_vehicles(in_file: str, out_file: str, keep_count: int) -> int:
    tree = ET.parse(in_file)
    root = tree.getroot()

    vehicles = root.findall("vehicle")
    if len(vehicles) < keep_count:
        raise RuntimeError(
            f"Route file has only {len(vehicles)} vehicles, cannot keep requested {keep_count}: {in_file}"
        )

    for v in vehicles[keep_count:]:
        root.remove(v)

    tree.write(out_file, encoding="utf-8", xml_declaration=True)
    return len(root.findall("vehicle"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Trim route file to exact number of vehicle entries")
    parser.add_argument("--in", dest="in_file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--count", type=int, required=True)
    args = parser.parse_args()

    if args.count < 1:
        raise RuntimeError("count must be >= 1")

    kept = trim_vehicles(args.in_file, args.out, args.count)
    print(f"Trimmed {args.out} to {kept} vehicles")


if __name__ == "__main__":
    main()
