import argparse
import json
import os
import socket
import sys
import time

from detection_fusion import DetectionConfig, DualVerificationDetector, read_float_file, read_wireless_file
from hospital_dispatch import HospitalRegistry, send_hospital_pre_notification
from live_ingestion import (
    derive_trigger_confidence,
    load_live_traffic,
    load_lora_events,
    map_match_lora_events,
    merge_routing_costs,
)
from traffic_police_dispatch import send_police_notification
from route_planner import (
    StaticGraphRouter,
    apply_vehicle_target,
    build_route_to_hospital,
    estimate_etas_for_hospitals,
    snapshot_live_edge_costs,
)
from signal_preemption import GreenCorridorController


def ensure_sumo_import():
    if "SUMO_HOME" not in os.environ:
        raise RuntimeError("Please set SUMO_HOME to your SUMO installation path.")

    tools = os.path.join(os.environ["SUMO_HOME"], "tools")
    if tools not in sys.path:
        sys.path.append(tools)

    import traci  # pylint: disable=import-outside-toplevel

    return traci


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_vehicle_ids(traci, manual_ids: list[str], use_all_emergency: bool) -> list[str]:
    if manual_ids:
        return [v for v in manual_ids if v in traci.vehicle.getIDList()]

    if not use_all_emergency:
        return []

    ids = []
    for veh_id in traci.vehicle.getIDList():
        try:
            if traci.vehicle.getTypeID(veh_id) == "emergency" or veh_id.startswith("ambulance_"):
                ids.append(veh_id)
        except Exception:
            continue
    return ids


def get_priority_score(vehicle_id: str, emergency_type: str) -> float:
    base = {
        "trauma": 1.0,
        "cardiac": 1.15,
        "stroke": 1.2,
        "general": 0.9,
    }.get(emergency_type, 1.0)

    # Slightly prioritize lower-numbered ambulance IDs for deterministic ties.
    if vehicle_id.startswith("ambulance_"):
        try:
            rank = int(vehicle_id.split("_")[-1])
            base += max(0.0, 0.05 - rank * 0.005)
        except ValueError:
            pass
    return base


def colorize_vehicle(traci, vehicle_id: str) -> None:
    # Bright color for emergency vehicle visibility in GUI.
    palette = [
        (255, 0, 0, 255),
        (255, 80, 0, 255),
        (255, 140, 0, 255),
        (220, 20, 60, 255),
        (255, 0, 128, 255),
    ]
    idx = 0
    if vehicle_id.startswith("ambulance_"):
        try:
            idx = (int(vehicle_id.split("_")[-1]) - 1) % len(palette)
        except ValueError:
            idx = 0
    try:
        traci.vehicle.setColor(vehicle_id, palette[idx])
    except Exception:
        pass


def configure_emergency_vehicle_runtime(traci, vehicle_id: str) -> None:
    # Best-effort runtime profile for emergency behavior and blue-light reactions.
    try:
        traci.vehicle.setParameter(vehicle_id, "has.bluelight.device", "true")
        traci.vehicle.setParameter(vehicle_id, "device.bluelight.reactiondist", "45")
        traci.vehicle.setSpeedFactor(vehicle_id, 1.12)
        traci.vehicle.setMinGap(vehicle_id, 2.2)
    except Exception:
        pass


def _resolve_gui_view_id(traci) -> str | None:
    try:
        view_ids = list(traci.gui.getIDList())
    except Exception:
        return None
    if not view_ids:
        return None
    return view_ids[0]


def apply_gui_declutter(traci, view_id: str) -> None:
    """Best-effort GUI cleanup so ambulances/signals stand out."""
    key_values = {
        "showPOILabels": "false",
        "showVehicleNames": "false",
        "showEdgeNames": "false",
        "showLaneNames": "false",
        "showJunctionNames": "false",
        "showDetectors": "false",
        "showGrid": "false",
    }
    for key, value in key_values.items():
        try:
            traci.gui.setParameter(view_id, key, value)
        except Exception:
            continue


def update_ambulance_debug_camera(
    traci,
    active_vehicles: list[str],
    state: dict,
    switch_interval_s: float,
    zoom: float,
    hide_non_emergency_labels: bool,
    follow_mode: str,
) -> None:
    if not active_vehicles:
        return

    sim_time = float(traci.simulation.getTime())
    view_id = state.get("view_id")
    if not view_id:
        view_id = _resolve_gui_view_id(traci)
        if not view_id:
            return
        state["view_id"] = view_id
        try:
            traci.gui.setSchema(view_id, "real world")
        except Exception:
            pass
        if hide_non_emergency_labels:
            apply_gui_declutter(traci, view_id)

    current_focus = state.get("focus_vehicle", "")
    last_switch_ts = float(state.get("last_switch_ts", -1e9))
    idx = int(state.get("idx", 0))

    if follow_mode == "fixed":
        should_switch = current_focus not in active_vehicles
    else:
        should_switch = (
            current_focus not in active_vehicles
            or (sim_time - last_switch_ts) >= switch_interval_s
        )

    if should_switch:
        idx = idx % len(active_vehicles)
        current_focus = active_vehicles[idx]
        idx = (idx + 1) % len(active_vehicles)
        state["idx"] = idx
        state["focus_vehicle"] = current_focus
        state["last_switch_ts"] = sim_time

    try:
        traci.gui.trackVehicle(view_id, current_focus)
        traci.gui.setZoom(view_id, float(zoom))
    except Exception:
        return

    # Optional pulsing highlight for focused ambulance; ignore if unsupported by SUMO build.
    try:
        traci.vehicle.highlight(current_focus, (255, 255, 0, 255), size=20.0, alphaMax=255, duration=2.0, type=1)
    except Exception:
        pass


def encourage_lane_clearance_for_ambulance(
    traci,
    ambulance_id: str,
    lookahead_m: float = 80.0,
    lane_change_duration_s: float = 2.0,
) -> None:
    """Best-effort: ask nearby non-emergency vehicles ahead to move left.

    SUMO lane index increases from right to left for standard right-hand traffic.
    """
    try:
        amb_edge = traci.vehicle.getRoadID(ambulance_id)
        if not amb_edge or amb_edge.startswith(":"):
            return
        amb_pos = float(traci.vehicle.getLanePosition(ambulance_id))
    except Exception:
        return

    try:
        edge_vehicle_ids = list(traci.edge.getLastStepVehicleIDs(amb_edge))
    except Exception:
        return

    for veh_id in edge_vehicle_ids:
        if veh_id == ambulance_id:
            continue
        try:
            vtype = traci.vehicle.getTypeID(veh_id)
            if vtype == "emergency" or veh_id.startswith("ambulance_"):
                continue

            veh_pos = float(traci.vehicle.getLanePosition(veh_id))
            if veh_pos <= amb_pos or (veh_pos - amb_pos) > lookahead_m:
                continue

            lane_id = traci.vehicle.getLaneID(veh_id)
            lane_index = int(traci.vehicle.getLaneIndex(veh_id))
            lane_ids = traci.edge.getLaneNumber(amb_edge)
            if lane_ids <= 1:
                continue

            # Try to move one lane left where possible.
            if lane_index < lane_ids - 1:
                traci.vehicle.changeLane(veh_id, lane_index + 1, float(lane_change_duration_s))
        except Exception:
            continue


def signal_color_name(raw_state: str) -> str:
    if not raw_state:
        return "unknown"
    ch = raw_state[0]
    if ch in {"r", "R"}:
        return "red"
    if ch in {"g", "G"}:
        return "green"
    if ch in {"y", "Y", "u", "U"}:
        return "yellow/orange"
    return f"other:{ch}"


def _tls_color_from_state(state: str) -> str:
    if not state:
        return "unknown"
    lowered = state.lower()
    if "g" in lowered:
        return "green"
    if "y" in lowered or "u" in lowered:
        return "yellow/orange"
    if "r" in lowered:
        return "red"
    return "unknown"


def log_status_panel(
    traci,
    vehicle_id: str,
    selected_plan: dict[str, dict],
    sim_time: float,
) -> None:
    speed_mps = 0.0
    try:
        speed_mps = float(traci.vehicle.getSpeed(vehicle_id))
    except Exception:
        pass

    tls_text = "none"

    try:
        next_tls = traci.vehicle.getNextTLS(vehicle_id)
        if next_tls:
            tls_id, _link, dist, state = next_tls[0]
            tls_text = f"{tls_id} dist={float(dist):.1f}m state={state} color={signal_color_name(state)}"
    except Exception:
        pass

    plan = selected_plan.get(vehicle_id, {})
    hospital_name = plan.get("hospital_name", "n/a")
    eta_s = plan.get("eta_seconds")
    eta_text = "n/a" if eta_s is None else f"{float(eta_s):.1f}s"

    print(
        "[PANEL] "
        f"t={sim_time:.1f}s focus={vehicle_id} speed={speed_mps * 3.6:.1f}km/h "
        f"next_tls=({tls_text}) hospital={hospital_name} eta={eta_text}"
    )


def log_assertion_metrics(
    sim_time: float,
    active_ambulances_count: int,
    active_tls_count: int,
    selected_plan_by_vehicle: dict[str, dict],
    active_vehicles: list[str],
) -> None:
    eta_vals = []
    for vehicle_id in active_vehicles:
        eta_s = selected_plan_by_vehicle.get(vehicle_id, {}).get("eta_seconds")
        if isinstance(eta_s, (int, float)):
            eta_vals.append(float(eta_s))
    avg_eta = (sum(eta_vals) / len(eta_vals)) if eta_vals else float("nan")
    avg_eta_text = "n/a" if avg_eta != avg_eta else f"{avg_eta:.1f}s"
    print(
        "[ASSERT] "
        f"t={sim_time:.1f}s active_ambulances={active_ambulances_count} "
        f"active_tls_preempted={active_tls_count} avg_hospital_eta={avg_eta_text}"
    )


def apply_hospital_stop(
    traci,
    vehicle_id: str,
    hospital_edge: str,
    hospital_id: str,
    stop_duration_s: float,
    hospital_stop_slots: dict[str, int],
) -> None:
    """Place ambulance stops in staggered lane/position slots to reduce hospital-edge blocking."""
    try:
        lane_count = int(traci.edge.getLaneNumber(hospital_edge))
    except Exception:
        lane_count = 1

    slot = int(hospital_stop_slots.get(hospital_id, 0))
    hospital_stop_slots[hospital_id] = slot + 1

    lane_index = 0 if lane_count <= 1 else (slot % lane_count)
    lane_id = f"{hospital_edge}_{lane_index}"

    try:
        lane_len = float(traci.lane.getLength(lane_id))
    except Exception:
        lane_len = 60.0

    # Spread queued ambulances backwards from lane end so they do not overlap at one point.
    pos = max(8.0, lane_len - 8.0 - (slot // max(1, lane_count)) * 14.0)

    try:
        traci.vehicle.setStop(
            vehicle_id,
            hospital_edge,
            pos=pos,
            laneIndex=lane_index,
            duration=float(stop_duration_s),
            flags=0,
        )
    except Exception:
        pass


def maybe_log_hospital_arrivals(
    traci,
    sim_time: float,
    active_vehicles: list[str],
    selected_plan_by_vehicle: dict[str, dict],
    hospital_edge_map: dict[str, str],
    dispatch_ts_by_vehicle: dict[str, float],
    reached_logged_by_vehicle: set[str],
    arrival_events: list[dict],
) -> None:
    for vehicle_id in active_vehicles:
        if vehicle_id in reached_logged_by_vehicle:
            continue
        plan = selected_plan_by_vehicle.get(vehicle_id)
        if not plan:
            continue

        hospital_id = str(plan.get("hospital_id", ""))
        target_edge = hospital_edge_map.get(hospital_id)
        if not target_edge:
            continue

        try:
            current_edge = traci.vehicle.getRoadID(vehicle_id)
            speed_mps = float(traci.vehicle.getSpeed(vehicle_id))
        except Exception:
            continue

        # Consider arrival when ambulance is on hospital edge and effectively stopped/creeping.
        if current_edge != target_edge or speed_mps > 0.5:
            continue

        dispatch_ts = float(dispatch_ts_by_vehicle.get(vehicle_id, sim_time))
        elapsed = max(0.0, sim_time - dispatch_ts)
        hospital_name = str(plan.get("hospital_name", hospital_id))
        print(f"[ARRIVAL] {vehicle_id} is reached (hospital={hospital_name}, t={sim_time:.1f}s, elapsed={elapsed:.1f}s)")
        reached_logged_by_vehicle.add(vehicle_id)
        arrival_events.append(
            {
                "vehicle_id": vehicle_id,
                "hospital_id": hospital_id,
                "hospital_name": hospital_name,
                "timestamp": round(sim_time, 1),
                "elapsed_seconds": round(elapsed, 1),
            }
        )


def update_breakdown_status(
    traci,
    sim_time: float,
    active_vehicles: list[str],
    reached_logged_by_vehicle: set[str],
    breakdown_logged_by_vehicle: set[str],
    stopped_since_by_vehicle: dict[str, float],
    stop_threshold_s: float,
    breakdown_events: list[dict],
) -> None:
    for vehicle_id in active_vehicles:
        if vehicle_id in reached_logged_by_vehicle or vehicle_id in breakdown_logged_by_vehicle:
            continue
        try:
            speed_mps = float(traci.vehicle.getSpeed(vehicle_id))
            edge_id = traci.vehicle.getRoadID(vehicle_id)
        except Exception:
            continue

        if edge_id.startswith(":"):
            stopped_since_by_vehicle.pop(vehicle_id, None)
            continue

        if speed_mps < 0.15:
            start_ts = stopped_since_by_vehicle.get(vehicle_id)
            if start_ts is None:
                stopped_since_by_vehicle[vehicle_id] = sim_time
            elif (sim_time - start_ts) >= stop_threshold_s:
                breakdown_logged_by_vehicle.add(vehicle_id)
                print(f"[BREAKDOWN] {vehicle_id} is breakdown (stopped_for={sim_time - start_ts:.1f}s, edge={edge_id})")
                breakdown_events.append(
                    {
                        "vehicle_id": vehicle_id,
                        "timestamp": round(sim_time, 1),
                        "edge": edge_id,
                        "stopped_for_seconds": round(sim_time - start_ts, 1),
                    }
                )
        else:
            stopped_since_by_vehicle.pop(vehicle_id, None)


def build_tls_snapshot(traci, controller: GreenCorridorController, only_preempted: bool = True) -> list[dict]:
    snapshot = []
    active_ids = set(controller.active_tls_ids())
    owner_map = controller.tls_owner_map()
    for tls_id in traci.trafficlight.getIDList():
        color = "unknown"
        try:
            state = traci.trafficlight.getRedYellowGreenState(tls_id)
            color = _tls_color_from_state(state)
        except Exception:
            pass

        lat = None
        lon = None
        try:
            junction_ids = traci.trafficlight.getControlledJunctions(tls_id)
            if junction_ids:
                x, y = traci.junction.getPosition(junction_ids[0])
                lon, lat = traci.simulation.convertGeo(x, y)
        except Exception:
            pass

        is_preempted = tls_id in active_ids
        if only_preempted and not is_preempted:
            continue

        snapshot.append(
            {
                "id": tls_id,
                "lat": lat,
                "lon": lon,
                "color": color,
                "preempted": is_preempted,
                "owner": owner_map.get(tls_id, ""),
            }
        )
    return snapshot


def write_web_state(
    traci,
    sim_time: float,
    active_vehicles: list[str],
    selected_plan_by_vehicle: dict[str, dict],
    reached_logged_by_vehicle: set[str],
    breakdown_logged_by_vehicle: set[str],
    arrival_events: list[dict],
    breakdown_events: list[dict],
    tls_snapshot: list[dict],
    active_tls_preempted_count: int,
    hospitals: list[dict],
    trigger_info: dict,
    selected_corridor_tls: list[str],
    police_events: list[dict],
    output_file: str,
) -> None:
    items = []
    for vehicle_id in active_vehicles:
        try:
            x, y = traci.vehicle.getPosition(vehicle_id)
            lon, lat = traci.simulation.convertGeo(x, y)
            speed_mps = float(traci.vehicle.getSpeed(vehicle_id))
        except Exception:
            continue

        plan = selected_plan_by_vehicle.get(vehicle_id, {})
        items.append(
            {
                "id": vehicle_id,
                "lat": float(lat),
                "lon": float(lon),
                "speed_kmh": round(speed_mps * 3.6, 2),
                "hospital_name": plan.get("hospital_name", ""),
                "eta_seconds": plan.get("eta_seconds"),
                "reroute_reason": plan.get("reroute_reason", ""),
                "police_notification_status": plan.get("police_notification_status", "pending"),
                "reached": vehicle_id in reached_logged_by_vehicle,
                "breakdown": vehicle_id in breakdown_logged_by_vehicle,
                "status": (
                    "reached"
                    if vehicle_id in reached_logged_by_vehicle
                    else ("breakdown" if vehicle_id in breakdown_logged_by_vehicle else "enroute")
                ),
            }
        )

    active_enroute = sum(1 for a in items if a.get("status") == "enroute")

    payload = {
        "timestamp": sim_time,
        "active_ambulances": active_enroute,
        "active_tls_preempted": active_tls_preempted_count,
        "ambulances": items,
        "arrivals": arrival_events[-50:],
        "breakdowns": breakdown_events[-50:],
        "signals": tls_snapshot,
        "hospitals": hospitals,
        "trigger": trigger_info,
        "selected_corridor_tls": selected_corridor_tls,
        "police_notifications": police_events[-50:],
    }
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(payload, f)


class LoRaUdpReceiver:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = int(port)
        self._sock = None
        if self.port <= 0:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.host, self.port))
            sock.setblocking(False)
            self._sock = sock
            print(f"[LORA] UDP listener active on {self.host}:{self.port}")
        except OSError as exc:
            print(f"[LORA] UDP listener unavailable: {exc}")
            self._sock = None

    def poll(self) -> bool:
        if self._sock is None:
            return False
        got_trigger = False
        while True:
            try:
                data, addr = self._sock.recvfrom(2048)
            except BlockingIOError:
                break
            except OSError:
                break

            text = data.decode("utf-8", errors="ignore").strip().lower()
            if text in {"1", "true", "yes", "on", "detected", "trigger", "siren"} or text:
                print(f"[LORA] trigger received from {addr[0]}:{addr[1]} payload='{text[:60]}'")
                got_trigger = True
        return got_trigger

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


def print_signal_inventory(traci) -> None:
    tls_ids = list(traci.trafficlight.getIDList())
    print(f"[SIGNAL] total_traffic_lights={len(tls_ids)}")
    if not tls_ids:
        print("[SIGNAL][WARN] No traffic lights found. Preemption cannot run.")
        return
    preview = ", ".join(tls_ids[:12])
    print(f"[SIGNAL] sample_tls_ids={preview}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hyderabad emergency green-corridor controller")
    parser.add_argument("--sumocfg", required=True, help="Path to SUMO configuration file")
    parser.add_argument("--sumo-binary", default="sumo-gui", help="sumo or sumo-gui")
    parser.add_argument("--vehicle-id", default="ambulance_1")
    parser.add_argument(
        "--vehicle-ids",
        default="",
        help="Comma-separated emergency vehicle IDs. If empty, uses --vehicle-id or auto-detect mode.",
    )
    parser.add_argument(
        "--auto-detect-emergency-vehicles",
        action="store_true",
        help="Control all active emergency vehicles found in the simulation.",
    )
    parser.add_argument("--emergency-type", default="trauma", choices=["trauma", "cardiac", "stroke", "general"])
    parser.add_argument("--mic-score-file", default="out/mic_score.txt")
    parser.add_argument("--wireless-file", default="out/wireless_signal.txt")
    parser.add_argument("--lora-udp-host", default="127.0.0.1", help="LoRa gateway UDP host")
    parser.add_argument("--lora-udp-port", type=int, default=0, help="LoRa gateway UDP port (0 disables UDP listener)")
    parser.add_argument(
        "--lora-events-file",
        default="out/lora_events.jsonl",
        help="Optional JSON/JSONL LoRa event stream file for realtime ingestion.",
    )
    parser.add_argument("--lora-events-max-age-s", type=float, default=8.0)
    parser.add_argument(
        "--live-traffic-file",
        default="out/live_traffic.json",
        help="Optional edge-level live traffic observations for routing-cost merge.",
    )
    parser.add_argument("--live-traffic-max-age-s", type=float, default=30.0)
    parser.add_argument("--routing-base-weight", type=float, default=0.7)
    parser.add_argument("--routing-live-weight", type=float, default=0.6)
    parser.add_argument("--routing-incident-penalty-s", type=float, default=45.0)
    parser.add_argument("--hospitals-csv", default="hyderabad_hospitals.csv")
    parser.add_argument(
        "--ambulance-debug-gui",
        action="store_true",
        help="In SUMO GUI, auto-track and highlight ambulances for visibility.",
    )
    parser.add_argument("--camera-switch-interval-s", type=float, default=6.0)
    parser.add_argument("--camera-zoom", type=float, default=1800.0)
    parser.add_argument("--camera-follow-mode", choices=["fixed", "cycle"], default="fixed")
    parser.add_argument(
        "--hide-non-emergency-labels",
        action="store_true",
        help="In GUI mode, hide non-emergency labels/overlays where supported.",
    )
    parser.add_argument("--status-panel-log", action="store_true", help="Print mini status panel logs for focused ambulance.")
    parser.add_argument("--status-panel-interval-s", type=float, default=1.0)
    parser.add_argument("--assert-interval-s", type=float, default=10.0)
    parser.add_argument("--hospital-stop-duration-s", type=float, default=180.0)
    parser.add_argument("--force-lane-clearance", action="store_true", help="Encourage nearby traffic to move left for ambulances")
    parser.add_argument("--lane-clearance-lookahead-m", type=float, default=80.0)
    parser.add_argument(
        "--force-green-corridor",
        action="store_true",
        help="Bypass dual verification and keep preemption/rerouting active for emergency vehicles.",
    )
    parser.add_argument(
        "--routing-algorithm",
        default="sumo",
        choices=["sumo", "dijkstra", "astar"],
        help="Shortest-path engine for hospital routing.",
    )
    parser.add_argument(
        "--routing-net-file",
        default="hyderabad.net.xml",
        help="SUMO net XML used by dijkstra/astar edge-graph routing.",
    )
    parser.add_argument("--config", default="config/hyderabad_example.json")
    parser.add_argument("--reroute-interval-s", type=float, default=5.0)
    parser.add_argument("--step-limit", type=int, default=0, help="0 means run until SUMO completes")
    parser.add_argument("--hospital-log", default="out/hospital_notifications.jsonl")
    parser.add_argument("--police-endpoint", default="", help="Optional HTTP endpoint for traffic police notifications")
    parser.add_argument("--police-log", default="out/police_notifications.jsonl")
    parser.add_argument("--police-notify-cooldown-s", type=float, default=15.0)
    parser.add_argument("--expected-ambulance-count", type=int, default=0)
    parser.add_argument("--breakdown-stop-threshold-s", type=float, default=70.0)
    parser.add_argument("--write-web-state", action="store_true")
    parser.add_argument("--web-state-file", default="out/realtime_state.json")
    parser.add_argument("--web-state-interval-s", type=float, default=1.0)
    parser.add_argument(
        "--profile",
        default="",
        help="Optional profile JSON to override runtime knobs (controller.reroute_interval_s, etc.).",
    )
    args = parser.parse_args()

    traci = ensure_sumo_import()
    cfg = load_config(args.config)
    profile = load_config(args.profile) if args.profile else {}

    controller_profile = profile.get("controller", {})

    detector = DualVerificationDetector(
        DetectionConfig(
            siren_threshold=float(cfg.get("siren_threshold", 0.75)),
            min_siren_hits=int(cfg.get("min_siren_hits", 3)),
            wireless_grace_seconds=float(cfg.get("wireless_grace_seconds", 4.0)),
            confirmation_cooldown_seconds=float(cfg.get("confirmation_cooldown_seconds", 8.0)),
        )
    )

    controller = GreenCorridorController(
        preemption_phases=cfg.get("preemption_phases", {}),
        lookahead_m=float(controller_profile.get("lookahead_m", cfg.get("lookahead_m", 350.0))),
        hold_green_s=float(controller_profile.get("hold_green_s", cfg.get("hold_green_s", 18.0))),
        restore_after_s=float(controller_profile.get("restore_after_s", cfg.get("restore_after_s", 20.0))),
        max_owner_hold_s=float(controller_profile.get("max_owner_hold_s", 35.0)),
        min_switch_interval_s=float(controller_profile.get("min_switch_interval_s", 5.0)),
    )

    hospital_edge_map = cfg.get("hospital_edge_map", {})
    hospitals = HospitalRegistry(args.hospitals_csv)
    hospital_points = [
        {"id": h.hospital_id, "name": h.name, "lat": h.lat, "lon": h.lon}
        for h in hospitals.hospitals
    ]
    static_router = None
    if args.routing_algorithm in {"dijkstra", "astar"}:
        static_router = StaticGraphRouter.from_net_file(args.routing_net_file)

    enable_debug_gui = bool(args.ambulance_debug_gui and args.sumo_binary == "sumo-gui")

    sumo_cmd = [
        args.sumo_binary,
        "-c",
        args.sumocfg,
        "--start",
        "--quit-on-end",
        "--collision.action",
        "warn",
        "--time-to-teleport",
        "60",
        "--time-to-teleport.highways",
        "30",
        "--log",
        "out/sumo_runtime.log",
    ]
    traci.start(sumo_cmd)
    lora = LoRaUdpReceiver(args.lora_udp_host, args.lora_udp_port)
    traci_disconnected = False

    print("[INFO] Simulation started")
    print(
        "[INFO] Rerouting: route_planner.py via "
        f"estimate_etas_for_hospitals/build_route_to_hospital/apply_vehicle_route ({args.routing_algorithm})"
    )
    print("[INFO] Dedicated priority: emergency vClass + blue-light device + signal_preemption.py green corridor")
    if args.force_green_corridor:
        print("[INFO] Force green corridor: enabled (verification bypass)")
    if enable_debug_gui:
        print(
            "[INFO] GUI debug camera: enabled "
            f"(follow_mode={args.camera_follow_mode}, ambulance auto-follow + highlight)"
        )
    if args.hide_non_emergency_labels and args.sumo_binary == "sumo-gui":
        print("[INFO] GUI declutter: hide non-emergency labels enabled")
    if args.status_panel_log:
        print("[INFO] Status panel logs: enabled")
    print_signal_inventory(traci)

    manual_ids = [v.strip() for v in args.vehicle_ids.split(",") if v.strip()]
    if not manual_ids and not args.auto_detect_emergency_vehicles and args.vehicle_id:
        manual_ids = [args.vehicle_id]

    reroute_interval_s = float(controller_profile.get("reroute_interval_s", args.reroute_interval_s))

    last_reroute_ts_by_vehicle: dict[str, float] = {}
    notified_hospital_id_by_vehicle: dict[str, str] = {}
    selected_plan_by_vehicle: dict[str, dict] = {}
    assigned_hospital_id_by_vehicle: dict[str, str] = {}
    hospital_load_by_id: dict[str, int] = {}
    hospital_stop_slots: dict[str, int] = {}
    route_fail_retry_after_ts_by_vehicle: dict[str, float] = {}
    dispatch_ts_by_vehicle: dict[str, float] = {}
    last_police_notify_ts_by_vehicle: dict[str, float] = {}
    reached_logged_by_vehicle: set[str] = set()
    breakdown_logged_by_vehicle: set[str] = set()
    stopped_since_by_vehicle: dict[str, float] = {}
    arrival_events: list[dict] = []
    breakdown_events: list[dict] = []
    police_events: list[dict] = []
    hospital_stop_set_by_vehicle: set[str] = set()
    runtime_configured_vehicles: set[str] = set()
    gui_camera_state: dict[str, object] = {"idx": 0, "last_switch_ts": -1e9, "focus_vehicle": "", "view_id": ""}
    trigger_info: dict = {"confidence": 0.0, "confirmed": False, "sources": [], "matched_lora_events": 0}
    selected_corridor_tls: list[str] = []
    last_panel_ts = -1e9
    last_assert_ts = -1e9
    last_web_state_ts = -1e9
    all_reached_logged = False
    steps = 0

    while traci.simulation.getMinExpectedNumber() > 0:
        try:
            traci.simulationStep()
        except Exception as exc:
            print(f"[WARN] Simulation step interrupted: {exc}")
            traci_disconnected = True
            break
        steps += 1

        if args.step_limit and steps >= args.step_limit:
            break

        active_vehicles = resolve_vehicle_ids(traci, manual_ids, args.auto_detect_emergency_vehicles)
        if not active_vehicles:
            controller.restore_finished_tls(traci)
            continue

        for v in active_vehicles:
            colorize_vehicle(traci, v)
            if v not in runtime_configured_vehicles:
                configure_emergency_vehicle_runtime(traci, v)
                runtime_configured_vehicles.add(v)

        sim_time = float(traci.simulation.getTime())
        update_breakdown_status(
            traci=traci,
            sim_time=sim_time,
            active_vehicles=active_vehicles,
            reached_logged_by_vehicle=reached_logged_by_vehicle,
            breakdown_logged_by_vehicle=breakdown_logged_by_vehicle,
            stopped_since_by_vehicle=stopped_since_by_vehicle,
            stop_threshold_s=float(args.breakdown_stop_threshold_s),
            breakdown_events=breakdown_events,
        )

        if enable_debug_gui:
            display_vehicles = [
                v for v in active_vehicles if v not in reached_logged_by_vehicle and v not in breakdown_logged_by_vehicle
            ]
            if not display_vehicles:
                display_vehicles = active_vehicles
            update_ambulance_debug_camera(
                traci,
                display_vehicles,
                gui_camera_state,
                switch_interval_s=float(args.camera_switch_interval_s),
                zoom=float(args.camera_zoom),
                hide_non_emergency_labels=bool(args.hide_non_emergency_labels),
                follow_mode=str(args.camera_follow_mode),
            )

        if steps % 50 == 0:
            print(f"[INFO] t={traci.simulation.getTime():.1f}s active_emergency={active_vehicles}")
            probe = active_vehicles[0]
            try:
                next_tls = traci.vehicle.getNextTLS(probe)
                if next_tls:
                    tls_id, _link, dist, state = next_tls[0]
                    print(f"[SIGNAL] probe={probe} next_tls={tls_id} distance={dist:.1f} state={state}")
                else:
                    print(f"[SIGNAL] probe={probe} has no upcoming traffic light in current route")
            except Exception:
                pass

        wall_now_ts = time.time()
        siren_score = read_float_file(args.mic_score_file, default=0.0)
        lora_udp_seen = lora.poll()
        lora_file_events = load_lora_events(
            args.lora_events_file,
            now_ts=wall_now_ts,
            max_age_s=float(args.lora_events_max_age_s),
        )
        matched_lora_events = map_match_lora_events(
            traci,
            lora_file_events,
            static_router.edge_center_xy if static_router is not None else {},
        )

        wireless_file_seen = read_wireless_file(args.wireless_file)
        wireless_seen = wireless_file_seen or lora_udp_seen or bool(matched_lora_events)
        geo_valid = bool(matched_lora_events)
        trigger_confidence = derive_trigger_confidence(
            siren_score=siren_score,
            wireless_seen=wireless_seen,
            geo_valid=geo_valid,
        )
        confirmed = detector.update(siren_score=siren_score, wireless_seen=wireless_seen, now_ts=wall_now_ts)

        trigger_sources = []
        if siren_score > 0.0:
            trigger_sources.append("siren")
        if wireless_file_seen:
            trigger_sources.append("wireless_file")
        if lora_udp_seen:
            trigger_sources.append("lora_udp")
        if matched_lora_events:
            trigger_sources.append("lora_map_matched")

        trigger_info = {
            "confidence": round(float(trigger_confidence), 3),
            "confirmed": bool(confirmed),
            "sources": trigger_sources,
            "matched_lora_events": len(matched_lora_events),
        }

        should_preempt = bool(args.force_green_corridor or confirmed)
        if should_preempt:
            now = traci.simulation.getTime()
            live_edge_costs = None
            if static_router is not None:
                sumo_costs = snapshot_live_edge_costs(traci, static_router)
                roadside_obs = load_live_traffic(
                    args.live_traffic_file,
                    now_ts=wall_now_ts,
                    max_age_s=float(args.live_traffic_max_age_s),
                )
                live_edge_costs = merge_routing_costs(
                    base_costs=sumo_costs,
                    traffic_by_edge=roadside_obs,
                    base_weight=float(args.routing_base_weight),
                    live_weight=float(args.routing_live_weight),
                    incident_penalty_s=float(args.routing_incident_penalty_s),
                )

            controllable_vehicles = [
                v for v in active_vehicles if v not in reached_logged_by_vehicle and v not in breakdown_logged_by_vehicle
            ]
            for vehicle_id in controllable_vehicles:
                last_ts = last_reroute_ts_by_vehicle.get(vehicle_id, -1e9)
                if now - last_ts < reroute_interval_s:
                    continue

                retry_after = route_fail_retry_after_ts_by_vehicle.get(vehicle_id, -1e9)
                if now < retry_after:
                    continue

                if args.force_lane_clearance:
                    encourage_lane_clearance_for_ambulance(
                        traci,
                        vehicle_id,
                        lookahead_m=float(args.lane_clearance_lookahead_m),
                    )

                try:
                    current_edge = traci.vehicle.getRoadID(vehicle_id)
                except Exception:
                    current_edge = ""
                if current_edge.startswith(":"):
                    # Skip route replacement while vehicle is on internal junction edges.
                    continue

                etas = estimate_etas_for_hospitals(
                    traci,
                    vehicle_id,
                    hospital_edge_map,
                    routing_algorithm=args.routing_algorithm,
                    router=static_router,
                    edge_cost_override_s=live_edge_costs,
                )
                # Penalize over-subscribed hospitals so fleet distributes better.
                eta_adjusted = {
                    hid: (float(eta) + float(hospital_load_by_id.get(hid, 0)) * 18.0)
                    for hid, eta in etas.items()
                }
                best = hospitals.select_best(args.emergency_type, eta_adjusted)

                assigned_id = assigned_hospital_id_by_vehicle.get(vehicle_id)
                if assigned_id and assigned_id in hospital_edge_map:
                    # Keep previously assigned destination to avoid route oscillation/jitter.
                    if eta_adjusted.get(assigned_id, float("inf")) < float("inf"):
                        best = next((h for h in hospitals.hospitals if h.hospital_id == assigned_id), best)
                if best and best.hospital_id in hospital_edge_map:
                    reroute_reason = "best_eta_capacity"
                    if assigned_id == best.hospital_id:
                        reroute_reason = "keep_assigned_stability"
                    elif static_router is not None and args.routing_algorithm in {"dijkstra", "astar"}:
                        reroute_reason = "switched_due_live_traffic"

                    plan = build_route_to_hospital(
                        traci,
                        vehicle_id,
                        hospital_edge_map[best.hospital_id],
                        best.hospital_id,
                        routing_algorithm=args.routing_algorithm,
                        router=static_router,
                        edge_cost_override_s=live_edge_costs,
                    )
                    if plan and apply_vehicle_target(
                        traci,
                        vehicle_id,
                        target_edge=hospital_edge_map[best.hospital_id],
                        fallback_route_edges=plan.route_edges,
                    ):
                        prev_assigned = assigned_hospital_id_by_vehicle.get(vehicle_id)
                        if prev_assigned and prev_assigned != best.hospital_id:
                            hospital_load_by_id[prev_assigned] = max(0, int(hospital_load_by_id.get(prev_assigned, 1)) - 1)
                        if prev_assigned != best.hospital_id:
                            hospital_load_by_id[best.hospital_id] = int(hospital_load_by_id.get(best.hospital_id, 0)) + 1
                        assigned_hospital_id_by_vehicle[vehicle_id] = best.hospital_id
                        if vehicle_id not in dispatch_ts_by_vehicle:
                            dispatch_ts_by_vehicle[vehicle_id] = float(now)
                        selected_plan_by_vehicle[vehicle_id] = {
                            "hospital_id": best.hospital_id,
                            "hospital_name": best.name,
                            "eta_seconds": plan.eta_seconds,
                            "reroute_reason": reroute_reason,
                            "police_notification_status": selected_plan_by_vehicle.get(vehicle_id, {}).get(
                                "police_notification_status",
                                "pending",
                            ),
                        }
                        if vehicle_id not in hospital_stop_set_by_vehicle:
                            apply_hospital_stop(
                                traci,
                                vehicle_id,
                                hospital_edge=hospital_edge_map[best.hospital_id],
                                hospital_id=best.hospital_id,
                                stop_duration_s=float(args.hospital_stop_duration_s),
                                hospital_stop_slots=hospital_stop_slots,
                            )
                            hospital_stop_set_by_vehicle.add(vehicle_id)
                        print(
                            f"[INFO] {vehicle_id} route -> {best.name} "
                            f"(eta={plan.eta_seconds:.1f}s, type={args.emergency_type})"
                        )
                        if notified_hospital_id_by_vehicle.get(vehicle_id) != best.hospital_id:
                            send_hospital_pre_notification(
                                best,
                                vehicle_id=vehicle_id,
                                emergency_type=args.emergency_type,
                                eta_seconds=plan.eta_seconds,
                                fallback_log_path=args.hospital_log,
                            )
                            notified_hospital_id_by_vehicle[vehicle_id] = best.hospital_id
                    else:
                        # Backoff retries to avoid repeated invalid updates at same junction point.
                        route_fail_retry_after_ts_by_vehicle[vehicle_id] = float(now) + 4.0

                last_reroute_ts_by_vehicle[vehicle_id] = now

            priority_by_vehicle = {v: get_priority_score(v, args.emergency_type) for v in controllable_vehicles}
            controller.preempt_for_vehicles(traci, controllable_vehicles, priority_by_vehicle)
            selected_corridor_tls = sorted(controller.active_tls_ids())

            for vehicle_id in controllable_vehicles:
                plan = selected_plan_by_vehicle.get(vehicle_id, {})
                eta_seconds = plan.get("eta_seconds")
                reroute_reason = str(plan.get("reroute_reason", "best_eta_capacity"))

                corridor_for_vehicle = [
                    tls_id
                    for tls_id, owner in controller.tls_owner_map().items()
                    if owner == vehicle_id
                ]

                last_police_ts = float(last_police_notify_ts_by_vehicle.get(vehicle_id, -1e9))
                if float(now) - last_police_ts < float(args.police_notify_cooldown_s):
                    continue

                police_result = send_police_notification(
                    endpoint=str(args.police_endpoint),
                    fallback_log_path=str(args.police_log),
                    vehicle_id=vehicle_id,
                    emergency_type=str(args.emergency_type),
                    eta_seconds=eta_seconds,
                    corridor_tls_ids=corridor_for_vehicle,
                    trigger_confidence=float(trigger_info.get("confidence", 0.0)),
                    reroute_reason=reroute_reason,
                )
                police_status = str(police_result.get("status", "unknown"))
                selected_plan_by_vehicle.setdefault(vehicle_id, {})["police_notification_status"] = police_status
                police_events.append(
                    {
                        "timestamp": round(float(sim_time), 1),
                        "vehicle_id": vehicle_id,
                        "status": police_status,
                        "corridor_tls": corridor_for_vehicle,
                        "reroute_reason": reroute_reason,
                    }
                )
                last_police_notify_ts_by_vehicle[vehicle_id] = float(now)
        else:
            controller.restore_finished_tls(traci)
            selected_corridor_tls = sorted(controller.active_tls_ids())

        sim_time = float(traci.simulation.getTime())
        maybe_log_hospital_arrivals(
            traci=traci,
            sim_time=sim_time,
            active_vehicles=active_vehicles,
            selected_plan_by_vehicle=selected_plan_by_vehicle,
            hospital_edge_map=hospital_edge_map,
            dispatch_ts_by_vehicle=dispatch_ts_by_vehicle,
            reached_logged_by_vehicle=reached_logged_by_vehicle,
            arrival_events=arrival_events,
        )

        # Release load counters for completed/broken-down ambulances.
        for vehicle_id in list(assigned_hospital_id_by_vehicle.keys()):
            if vehicle_id not in reached_logged_by_vehicle and vehicle_id not in breakdown_logged_by_vehicle:
                continue
            hid = assigned_hospital_id_by_vehicle.pop(vehicle_id, None)
            if hid:
                hospital_load_by_id[hid] = max(0, int(hospital_load_by_id.get(hid, 1)) - 1)
        if args.status_panel_log and (sim_time - last_panel_ts) >= float(args.status_panel_interval_s):
            focus_vehicle = str(gui_camera_state.get("focus_vehicle", ""))
            preferred = [v for v in active_vehicles if v not in reached_logged_by_vehicle and v not in breakdown_logged_by_vehicle]
            if not preferred:
                preferred = active_vehicles
            if not focus_vehicle or focus_vehicle not in preferred:
                focus_vehicle = preferred[0]
            log_status_panel(traci, focus_vehicle, selected_plan_by_vehicle, sim_time)
            last_panel_ts = sim_time

        if (sim_time - last_assert_ts) >= float(args.assert_interval_s):
            log_assertion_metrics(
                sim_time=sim_time,
                active_ambulances_count=len(active_vehicles),
                active_tls_count=controller.active_tls_count(),
                selected_plan_by_vehicle=selected_plan_by_vehicle,
                active_vehicles=active_vehicles,
            )
            last_assert_ts = sim_time

        if args.write_web_state and (sim_time - last_web_state_ts) >= float(args.web_state_interval_s):
            tls_snapshot = build_tls_snapshot(traci, controller, only_preempted=True)
            write_web_state(
                traci=traci,
                sim_time=sim_time,
                active_vehicles=active_vehicles,
                selected_plan_by_vehicle=selected_plan_by_vehicle,
                reached_logged_by_vehicle=reached_logged_by_vehicle,
                breakdown_logged_by_vehicle=breakdown_logged_by_vehicle,
                arrival_events=arrival_events,
                breakdown_events=breakdown_events,
                tls_snapshot=tls_snapshot,
                active_tls_preempted_count=controller.active_tls_count(),
                hospitals=hospital_points,
                trigger_info=trigger_info,
                selected_corridor_tls=selected_corridor_tls,
                police_events=police_events,
                output_file=args.web_state_file,
            )
            last_web_state_ts = sim_time

        if not all_reached_logged and args.expected_ambulance_count > 0:
            if len(reached_logged_by_vehicle) >= int(args.expected_ambulance_count):
                print(f"[ARRIVAL] all ambulances are reached (count={len(reached_logged_by_vehicle)})")
                all_reached_logged = True
            elif (len(reached_logged_by_vehicle) + len(breakdown_logged_by_vehicle)) >= int(args.expected_ambulance_count):
                print(
                    "[SUMMARY] all ambulances completed "
                    f"(reached={len(reached_logged_by_vehicle)} breakdown={len(breakdown_logged_by_vehicle)})"
                )
                all_reached_logged = True

    if not traci_disconnected:
        try:
            controller.restore_finished_tls(traci, force_all=True)
        except Exception as exc:
            print(f"[WARN] Failed final TLS restore: {exc}")
    lora.close()
    if not traci_disconnected:
        try:
            traci.close()
        except Exception:
            pass
    else:
        print("[WARN] SUMO disconnected early. Check out/sumo_runtime.log for root cause.")
    print("[INFO] Simulation ended")


if __name__ == "__main__":
    main()
