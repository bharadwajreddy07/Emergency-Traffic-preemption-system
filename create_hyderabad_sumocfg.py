import argparse
import os


SUMOCFG_TEMPLATE = """<configuration>
  <input>
    <net-file value=\"{net_file}\"/>
    <route-files value=\"{route_files}\"/>
    {additional_line}
  </input>
  <time>
    <begin value=\"0\"/>
    <end value=\"{end_time}\"/>
    <step-length value=\"{step_length}\"/>
  </time>
  <processing>
    <ignore-route-errors value=\"true\"/>
  </processing>
  <report>
    <verbose value=\"false\"/>
    <no-step-log value=\"true\"/>
  </report>
</configuration>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Create hyderabad.sumocfg file")
    parser.add_argument("--net-file", default="hyderabad.net.xml")
    parser.add_argument(
        "--route-files",
        default="hyderabad.rou.xml",
        help="Comma-separated route files, e.g. hyderabad.rou.xml,emergency_vehicle.rou.xml",
    )
    parser.add_argument(
      "--additional-files",
      default="",
      help="Optional comma-separated additional files, e.g. hospital_markers.add.xml",
    )
    parser.add_argument("--out", default="hyderabad.sumocfg")
    parser.add_argument("--end-time", type=int, default=7200)
    parser.add_argument("--step-length", type=float, default=1.0)
    args = parser.parse_args()

    additional_line = ""
    if args.additional_files.strip():
      additional_line = f'<additional-files value="{args.additional_files.strip()}"/>'

    content = SUMOCFG_TEMPLATE.format(
        net_file=args.net_file,
        route_files=args.route_files,
      additional_line=additional_line,
        end_time=args.end_time,
        step_length=args.step_length,
    )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Wrote SUMO config: {args.out}")


if __name__ == "__main__":
    main()
