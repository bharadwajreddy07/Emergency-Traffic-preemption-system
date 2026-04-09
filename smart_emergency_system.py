import argparse
import json
import math
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


_LAST_CORRIDOR_VIS: dict[str, object] = {
    "timestamp": -1.0,
    "selected_corridor_tls": [],
    "planned_corridor_tls": [],
    "planned_corridor_mode": "strict",
    "corridor_route": [],
    "corridor_source": "none",
}

ALLOWED_HEALTH_EMERGENCY_TYPES = {"trauma", "cardiac", "stroke", "general"}


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


def geo_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(1e-12, 1.0 - a)))
    return float(r * c)


def build_driver_profile(vehicle_id: str) -> dict:
    first_names = [
        "Arun", "Kiran", "Naveen", "Suresh", "Ravi", "Prakash", "Rahul", "Imran", "Naresh", "Ajay",
        "Mahesh", "Sunil", "Vikram", "Manoj", "Harish", "Ramesh", "Dinesh", "Faiz", "Anil", "Karthik",
    ]
    last_names = [
        "Reddy", "Kumar", "Sharma", "Verma", "Naidu", "Patel", "Rao", "Yadav", "Singh", "Ali",
    ]

    seed = 0
    for ch in str(vehicle_id):
        seed = (seed * 31 + ord(ch)) % 10_000

    first = first_names[seed % len(first_names)]
    last = last_names[(seed // 7) % len(last_names)]
    phone = f"+91-9000{seed % 100000:05d}"
    return {
        "driver_name": f"{first} {last}",
        "driver_phone": phone,
        "driver_license": f"TS-EMS-{seed:04d}",
    }


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
    priority_vehicles: list[str] | None = None,
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
            # Standard schema keeps signal-state visualization clearer for ops.
            traci.gui.setSchema(view_id, "standard")
        except Exception:
            pass
        if hide_non_emergency_labels:
            apply_gui_declutter(traci, view_id)

    current_focus = state.get("focus_vehicle", "")
    last_switch_ts = float(state.get("last_switch_ts", -1e9))
    idx = int(state.get("idx", 0))
    priority = [v for v in (priority_vehicles or []) if v in active_vehicles]
    focus_pool = priority if priority else active_vehicles

    if follow_mode == "fleet":
        xs: list[float] = []
        ys: list[float] = []
        for vehicle_id in focus_pool:
            try:
                x, y = traci.vehicle.getPosition(vehicle_id)
                xs.append(float(x))
                ys.append(float(y))
            except Exception:
                continue

        if not xs or not ys:
            return

        min_x = min(xs)
        max_x = max(xs)
        min_y = min(ys)
        max_y = max(ys)
        pad = 180.0

        try:
            # Disable single-vehicle lock and show all active emergency vehicles.
            traci.gui.trackVehicle(view_id, "")
        except Exception:
            pass
        try:
            traci.gui.setBoundary(view_id, min_x - pad, min_y - pad, max_x + pad, max_y + pad)
        except Exception:
            pass

        state["focus_vehicle"] = ""
        return

    if follow_mode == "fixed":
        # In fixed mode, keep stable focus but still jump to an active mission vehicle.
        should_switch = current_focus not in active_vehicles or (priority and current_focus not in set(priority))
    else:
        should_switch = (
            current_focus not in active_vehicles
            or (sim_time - last_switch_ts) >= switch_interval_s
        )

    if should_switch:
        idx = idx % len(focus_pool)
        current_focus = focus_pool[idx]
        idx = (idx + 1) % len(focus_pool)
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


def _resolve_tls_anchor_xy(traci, tls_id: str) -> tuple[float, float] | None:
    try:
        junction_ids = traci.trafficlight.getControlledJunctions(tls_id)
    except Exception:
        junction_ids = []

    for jid in junction_ids:
        try:
            x, y = traci.junction.getPosition(jid)
            return float(x), float(y)
        except Exception:
            continue

    try:
        lanes = list(traci.trafficlight.getControlledLanes(tls_id))
    except Exception:
        lanes = []

    for lane_id in lanes:
        try:
            shape = traci.lane.getShape(lane_id)
        except Exception:
            continue
        if not shape:
            continue
        try:
            x, y = shape[0]
            return float(x), float(y)
        except Exception:
            continue

    return None


def sync_preempted_tls_gui_markers(
    traci,
    active_tls_ids: list[str],
    owner_by_tls: dict[str, str] | None,
    state: dict,
) -> None:
    owner_map = {str(k): str(v) for k, v in (owner_by_tls or {}).items()}

    palette = [
        (255, 64, 64, 255),
        (64, 160, 255, 255),
        (64, 220, 128, 255),
        (255, 196, 64, 255),
        (190, 120, 255, 255),
        (255, 128, 180, 255),
    ]

    def owner_color(owner_id: str) -> tuple[int, int, int, int]:
        owner_id = str(owner_id or "")
        if owner_id.startswith("ambulance_"):
            try:
                n = int(owner_id.split("_")[-1])
                return palette[(max(1, n) - 1) % len(palette)]
            except ValueError:
                pass
        if not owner_id:
            return (255, 215, 0, 255)
        seed = 0
        for ch in owner_id:
            seed = (seed * 31 + ord(ch)) % 10_000
        return palette[seed % len(palette)]

    shown_tls = set(state.get("shown_tls", []))
    active_tls = {str(t) for t in (active_tls_ids or []) if str(t).strip()}

    for tls_id in sorted(shown_tls - active_tls):
        poi_id = f"preempt_tls::{tls_id}"
        try:
            traci.poi.remove(poi_id)
        except Exception:
            pass

    for tls_id in sorted(active_tls - shown_tls):
        anchor = _resolve_tls_anchor_xy(traci, tls_id)
        if not anchor:
            continue
        x, y = anchor
        poi_id = f"preempt_tls::{tls_id}"
        owner_id = owner_map.get(tls_id, "")
        try:
            traci.poi.add(
                poi_id,
                float(x),
                float(y),
                owner_color(owner_id),
                "preempted_tls",
                220,
            )
            traci.poi.setWidth(poi_id, 28.0)
            traci.poi.setHeight(poi_id, 28.0)
            traci.poi.setParameter(poi_id, "name", f"TLS {tls_id} ({owner_id or 'unassigned'})")
        except Exception:
            continue

    state["shown_tls"] = sorted(active_tls)


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
    vehicle_mission_by_id: dict[str, dict],
    stopped_since_by_vehicle: dict[str, float],
    stop_threshold_s: float,
    breakdown_events: list[dict],
) -> None:
    for vehicle_id in active_vehicles:
        if vehicle_id in reached_logged_by_vehicle or vehicle_id in breakdown_logged_by_vehicle:
            continue
        mission = vehicle_mission_by_id.get(vehicle_id)
        # Ambulances parked at hospitals are expected to be idle until dispatch.
        if not mission:
            stopped_since_by_vehicle.pop(vehicle_id, None)
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
    vehicle_mission_by_id: dict[str, dict],
    driver_details_by_vehicle: dict[str, dict],
    vehicle_home_hospital_by_id: dict[str, str],
    hospital_name_by_id: dict[str, str],
    reached_logged_by_vehicle: set[str],
    breakdown_logged_by_vehicle: set[str],
    arrival_events: list[dict],
    breakdown_events: list[dict],
    tls_snapshot: list[dict],
    active_tls_preempted_count: int,
    hospitals: list[dict],
    trigger_info: dict,
    live_feed_info: dict,
    selected_corridor_tls: list[str],
    planned_corridor_tls: list[str],
    planned_corridor_mode: str,
    police_events: list[dict],
    call_events: list[dict],
    call_markers: list[dict],
    edge_center_xy: dict[str, tuple[float, float]],
    output_file: str,
) -> None:
    global _LAST_CORRIDOR_VIS

    items = []
    visible_call_markers: list[dict] = []
    corridor_route: list[list[float]] = []
    corridor_source = "none"
    for vehicle_id in active_vehicles:
        try:
            x, y = traci.vehicle.getPosition(vehicle_id)
            lon, lat = traci.simulation.convertGeo(x, y)
            speed_mps = float(traci.vehicle.getSpeed(vehicle_id))
        except Exception:
            continue

        plan = selected_plan_by_vehicle.get(vehicle_id, {})
        mission = vehicle_mission_by_id.get(vehicle_id, {})
        driver = driver_details_by_vehicle.get(vehicle_id, {})
        home_hospital_id = str(vehicle_home_hospital_by_id.get(vehicle_id, "")).strip()
        home_hospital_name = str(hospital_name_by_id.get(home_hospital_id, home_hospital_id or ""))
        return_hospital_id = str(mission.get("return_hospital_id", "")).strip()
        return_hospital_name = str(hospital_name_by_id.get(return_hospital_id, return_hospital_id or ""))
        destination_hospital_id = str(plan.get("hospital_id", "")).strip() or return_hospital_id or home_hospital_id
        destination_hospital_name = (
            str(plan.get("hospital_name", "")).strip()
            or return_hospital_name
            or home_hospital_name
            or destination_hospital_id
        )
        live_route_preview = build_route_preview_geo(
            traci,
            vehicle_id,
            edge_center_xy=edge_center_xy,
        )
        route_preview = live_route_preview
        route_source = "live_route"
        if not route_preview:
            route_preview = route_edges_to_geo(
                traci,
                route_edges=plan.get("route_edges") or [],
                edge_center_xy=edge_center_xy,
            )
            route_source = "planned_route" if route_preview else "none"
        if not corridor_route and route_preview:
            corridor_route = route_preview[:]
            corridor_source = route_source

        mission_phase = str(mission.get("phase", "idle"))
        if vehicle_id in reached_logged_by_vehicle:
            status = "reached"
        elif vehicle_id in breakdown_logged_by_vehicle:
            status = "breakdown"
        elif mission_phase in {"to_incident", "to_hospital"}:
            status = "enroute"
        else:
            status = "stationed"

        items.append(
            {
                "id": vehicle_id,
                "lat": float(lat),
                "lon": float(lon),
                "speed_kmh": round(speed_mps * 3.6, 2),
                "hospital_name": plan.get("hospital_name", ""),
                "destination_hospital_id": destination_hospital_id,
                "destination_hospital_name": destination_hospital_name,
                "eta_seconds": plan.get("eta_seconds"),
                "reroute_reason": plan.get("reroute_reason", ""),
                "police_notification_status": plan.get("police_notification_status", "pending"),
                "mission_phase": mission_phase,
                "home_hospital_id": home_hospital_id,
                "home_hospital_name": home_hospital_name,
                "driver_name": driver.get("driver_name", ""),
                "driver_phone": driver.get("driver_phone", ""),
                "driver_license": driver.get("driver_license", ""),
                "reached": vehicle_id in reached_logged_by_vehicle,
                "breakdown": vehicle_id in breakdown_logged_by_vehicle,
                "status": status,
                "call_id": str(mission.get("call_id", "")),
                "incident_edge": str(mission.get("incident_edge", "")),
                "preferred_hospital_id": str(mission.get("preferred_hospital_id", "")),
                "return_hospital_id": return_hospital_id,
                "return_hospital_name": return_hospital_name,
                "route_source": route_source,
                "route_preview": route_preview,
            }
        )

    active_available = sum(1 for a in items if a.get("status") in {"stationed", "enroute"})

    selected_now = list(selected_corridor_tls or [])
    planned_now = list(planned_corridor_tls or [])
    route_now = list(corridor_route or [])
    source_now = str(corridor_source or "none")
    mode_now = str(planned_corridor_mode or "strict")

    if source_now == "none":
        if selected_now:
            source_now = "active_tls_only"
        elif planned_now:
            source_now = "planned_tls_only"

    if selected_now or planned_now or route_now:
        _LAST_CORRIDOR_VIS = {
            "timestamp": float(sim_time),
            "selected_corridor_tls": selected_now,
            "planned_corridor_tls": planned_now,
            "planned_corridor_mode": mode_now,
            "corridor_route": route_now,
            "corridor_source": source_now,
        }
    else:
        last_ts = float(_LAST_CORRIDOR_VIS.get("timestamp", -1.0) or -1.0)
        if sim_time - last_ts <= 120.0:
            selected_now = list(_LAST_CORRIDOR_VIS.get("selected_corridor_tls") or [])
            planned_now = list(_LAST_CORRIDOR_VIS.get("planned_corridor_tls") or [])
            route_now = list(_LAST_CORRIDOR_VIS.get("corridor_route") or [])
            source_now = "held_snapshot"
            mode_now = str(_LAST_CORRIDOR_VIS.get("planned_corridor_mode") or "strict")

    for marker in (call_markers or []):
        status = str(marker.get("status", "")).strip().lower()
        # Remove caller red marker as soon as ambulance reaches caller/pickup.
        if status in {"picked_up", "picked_up_auto", "closed_reached", "closed_breakdown", "reset_for_redispatch"}:
            continue
        visible_call_markers.append(marker)

    payload = {
        "timestamp": sim_time,
        "active_ambulances": active_available,
        "active_tls_preempted": active_tls_preempted_count,
        "ambulances": items,
        "arrivals": arrival_events[-50:],
        "breakdowns": breakdown_events[-50:],
        "signals": tls_snapshot,
        "hospitals": hospitals,
        "trigger": trigger_info,
        "live_feeds": live_feed_info,
        "selected_corridor_tls": selected_now,
        "planned_corridor_tls": planned_now,
        "planned_corridor_mode": mode_now,
        "corridor_route": route_now,
        "corridor_source": source_now,
        "police_notifications": police_events[-50:],
        "calls": call_events[-100:],
        "call_markers": visible_call_markers[-100:],
    }
    output_path = str(output_file or "out/realtime_state.json").strip() or "out/realtime_state.json"
    if "\x00" in output_path:
        output_path = "out/realtime_state.json"
    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as exc:
        fallback = os.path.abspath("out/realtime_state.json")
        os.makedirs(os.path.dirname(fallback) or ".", exist_ok=True)
        with open(fallback, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        print(f"[WARN] Failed writing web state to {output_path!r}: {exc}. Fallback used: {fallback}")


def read_new_call_requests(path: str, line_offset: int) -> tuple[list[dict], int]:
    if not path or not os.path.exists(path):
        return [], line_offset

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return [], line_offset

    if line_offset < 0:
        line_offset = 0
    if line_offset >= len(lines):
        return [], len(lines)

    events: list[dict] = []
    for raw in lines[line_offset:]:
        text = raw.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)

    return events, len(lines)


def _normalize_vehicle_ids_from_command(cmd: dict) -> set[str]:
    raw = cmd.get("vehicle_ids", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(v).strip() for v in raw if str(v).strip()}


def map_match_call_to_edge(traci, lat: float, lon: float, edge_center_xy: dict[str, tuple[float, float]]) -> str:
    if not edge_center_xy:
        return ""
    try:
        x, y = traci.simulation.convertGeo(float(lon), float(lat), True)
    except Exception:
        return ""

    best_edge = ""
    best_d2 = float("inf")
    for edge_id, (cx, cy) in edge_center_xy.items():
        d2 = (float(cx) - float(x)) ** 2 + (float(cy) - float(y)) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_edge = edge_id
    return best_edge


def build_route_preview_geo(
    traci,
    vehicle_id: str,
    edge_center_xy: dict[str, tuple[float, float]],
    max_points: int = 90,
) -> list[list[float]]:
    """Create a lightweight geo polyline for the remaining route for web visualization."""
    try:
        route_edges = list(traci.vehicle.getRoute(vehicle_id))
        route_index = int(traci.vehicle.getRouteIndex(vehicle_id))
    except Exception:
        return []

    points: list[list[float]] = []
    last_ll = None

    def append_lane_shape(edge_id: str) -> None:
        nonlocal last_ll
        if len(points) >= max_points:
            return
        try:
            lane_count = int(traci.edge.getLaneNumber(edge_id))
        except Exception:
            lane_count = 0
        if lane_count <= 0:
            return
        try:
            shape = traci.lane.getShape(f"{edge_id}_0")
        except Exception:
            shape = []
        for x, y in shape:
            if len(points) >= max_points:
                break
            try:
                lon, lat = traci.simulation.convertGeo(float(x), float(y))
            except Exception:
                continue
            ll = [float(lat), float(lon)]
            if last_ll is None or (abs(last_ll[0] - ll[0]) > 1e-7 or abs(last_ll[1] - ll[1]) > 1e-7):
                points.append(ll)
                last_ll = ll

    for edge_id in route_edges[max(0, route_index):]:
        if len(points) >= max_points:
            break
        if not edge_id or edge_id.startswith(":"):
            continue
        append_lane_shape(edge_id)

    return points


def route_edges_to_geo(
    traci,
    route_edges: list[str],
    edge_center_xy: dict[str, tuple[float, float]],
    max_points: int = 90,
) -> list[list[float]]:
    if not route_edges:
        return []

    points: list[list[float]] = []
    last_ll = None

    def append_lane_shape(edge_id: str) -> None:
        nonlocal last_ll
        if len(points) >= max_points:
            return
        try:
            lane_count = int(traci.edge.getLaneNumber(edge_id))
        except Exception:
            lane_count = 0
        if lane_count <= 0:
            return
        try:
            shape = traci.lane.getShape(f"{edge_id}_0")
        except Exception:
            shape = []
        for x, y in shape:
            if len(points) >= max_points:
                break
            try:
                lon, lat = traci.simulation.convertGeo(float(x), float(y))
            except Exception:
                continue
            ll = [float(lat), float(lon)]
            if last_ll is None or (abs(last_ll[0] - ll[0]) > 1e-7 or abs(last_ll[1] - ll[1]) > 1e-7):
                points.append(ll)
                last_ll = ll

    for edge_id in route_edges:
        if len(points) >= max_points:
            break
        if not edge_id or edge_id.startswith(":"):
            continue
        append_lane_shape(edge_id)

    return points


def build_tls_incoming_edge_index(traci) -> dict[str, set[str]]:
    """Build edge->TLS map from controlled incoming lanes for fast corridor candidate lookup."""
    edge_to_tls: dict[str, set[str]] = {}
    for tls_id in traci.trafficlight.getIDList():
        try:
            controlled_links = traci.trafficlight.getControlledLinks(tls_id)
        except Exception:
            continue
        for group in controlled_links:
            for link in group:
                if not link:
                    continue
                incoming_lane = str(link[0]) if len(link) > 0 else ""
                if not incoming_lane:
                    continue
                edge_id = incoming_lane.split("_")[0]
                if not edge_id or edge_id.startswith(":"):
                    continue
                edge_to_tls.setdefault(edge_id, set()).add(tls_id)
    return edge_to_tls


def extract_planned_corridor_tls(
    traci,
    vehicle_ids: list[str],
    tls_edge_index: dict[str, set[str]] | None = None,
    max_tls: int = 40,
) -> list[str]:
    """Collect upcoming corridor TLS ids from current ambulance route edges, even before active preemption."""
    ordered: list[str] = []
    seen: set[str] = set()
    for vehicle_id in vehicle_ids:
        # Primary: ask SUMO for upcoming TLS directly on the current route.
        try:
            next_tls = traci.vehicle.getNextTLS(vehicle_id)
        except Exception:
            next_tls = []
        for entry in next_tls:
            try:
                tls_id = str(entry[0])
            except Exception:
                continue
            if not tls_id or tls_id in seen:
                continue
            seen.add(tls_id)
            ordered.append(tls_id)
            if len(ordered) >= int(max_tls):
                return ordered

        if not tls_edge_index:
            continue

        # Fallback: scan remaining route edges and map incoming edges to TLS ids.
        try:
            route_edges = list(traci.vehicle.getRoute(vehicle_id))
            route_index = int(traci.vehicle.getRouteIndex(vehicle_id))
        except Exception:
            continue

        for edge_id in route_edges[max(0, route_index):]:
            if not edge_id or edge_id.startswith(":"):
                continue
            for tls_id in sorted(tls_edge_index.get(edge_id, set())):
                if tls_id in seen:
                    continue
                seen.add(tls_id)
                ordered.append(tls_id)
                if len(ordered) >= int(max_tls):
                    return ordered
    return ordered


def extract_relaxed_demo_planned_tls(
    traci,
    vehicle_ids: list[str],
    max_tls: int = 8,
    radius_m: float = 2500.0,
) -> list[str]:
    """Visualization-only fallback: nearest TLS candidates by distance from active ambulances."""
    if not vehicle_ids:
        return []

    vehicle_positions: list[tuple[float, float]] = []
    for vehicle_id in vehicle_ids:
        try:
            x, y = traci.vehicle.getPosition(vehicle_id)
        except Exception:
            continue
        vehicle_positions.append((float(x), float(y)))

    if not vehicle_positions:
        return []

    candidates: list[tuple[float, str]] = []
    for tls_id in traci.trafficlight.getIDList():
        tx = None
        ty = None
        try:
            junction_ids = traci.trafficlight.getControlledJunctions(tls_id)
            if not junction_ids:
                raise RuntimeError("no controlled junction")
            tx, ty = traci.junction.getPosition(junction_ids[0])
        except Exception:
            try:
                lanes = list(traci.trafficlight.getControlledLanes(tls_id))
            except Exception:
                lanes = []
            for lane_id in lanes:
                try:
                    shape = traci.lane.getShape(lane_id)
                except Exception:
                    continue
                if shape:
                    tx, ty = shape[0]
                    break

        if tx is None or ty is None:
            continue

        best_d2 = float("inf")
        for vx, vy in vehicle_positions:
            d2 = (float(tx) - vx) ** 2 + (float(ty) - vy) ** 2
            if d2 < best_d2:
                best_d2 = d2

        if best_d2 <= float(radius_m) ** 2:
            candidates.append((best_d2, str(tls_id)))

    candidates.sort(key=lambda item: item[0])
    return [tls_id for _d2, tls_id in candidates[: max(1, int(max_tls))]]


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
    parser.add_argument("--vehicle-id", default="")
    parser.add_argument(
        "--vehicle-ids",
        default="",
        help="Comma-separated emergency vehicle IDs. If empty, uses --vehicle-id or auto-detect mode.",
    )
    parser.add_argument(
        "--auto-detect-emergency-vehicles",
        action="store_true",
        default=True,
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
    parser.add_argument("--camera-follow-mode", choices=["fixed", "cycle", "fleet"], default="fleet")
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
    parser.add_argument(
        "--preemption-all-red-s",
        type=float,
        default=2.0,
        help="All-red safety hold before emergency green phase (seconds).",
    )
    parser.add_argument("--preemption-yellow-s", type=float, default=3.0)
    parser.add_argument("--min-owner-hold-s", type=float, default=12.0)
    parser.add_argument("--post-restore-cooldown-s", type=float, default=8.0)
    parser.add_argument("--min-dynamic-green-s", type=float, default=18.0)
    parser.add_argument("--max-dynamic-green-s", type=float, default=70.0)
    parser.add_argument("--queue-gain-s", type=float, default=1.4)
    parser.add_argument("--reroute-interval-s", type=float, default=5.0)
    parser.add_argument(
        "--reroute-min-dwell-s",
        type=float,
        default=30.0,
        help="Minimum time to keep current hospital assignment before allowing a switch.",
    )
    parser.add_argument(
        "--reroute-switch-eta-improvement-s",
        type=float,
        default=20.0,
        help="Required ETA improvement (seconds) before switching assigned hospital.",
    )
    parser.add_argument(
        "--preferred-hospital-max-extra-eta-s",
        type=float,
        default=180.0,
        help="Allow preferred hospital if ETA is within this extra time window over best ETA.",
    )
    parser.add_argument("--step-limit", type=int, default=0, help="0 means run until SUMO completes")
    parser.add_argument("--hospital-log", default="out/hospital_notifications.jsonl")
    parser.add_argument("--call-requests-file", default="out/call_requests.jsonl")
    parser.add_argument("--trip-commands-file", default="out/trip_commands.jsonl")
    parser.add_argument("--control-commands-file", default="out/control_commands.jsonl")
    parser.add_argument("--dispatch-cooldown-s", type=float, default=6.0)
    parser.add_argument("--police-endpoint", default="", help="Optional HTTP endpoint for traffic police notifications")
    parser.add_argument("--police-sms-to", default="", help="Optional police mobile number for SMS alerts (E.164).")
    parser.add_argument("--police-log", default="out/police_notifications.jsonl")
    parser.add_argument("--police-notify-cooldown-s", type=float, default=15.0)
    parser.add_argument("--expected-ambulance-count", type=int, default=0)
    parser.add_argument("--breakdown-stop-threshold-s", type=float, default=70.0)
    parser.add_argument("--write-web-state", action="store_true")
    parser.add_argument("--web-state-file", default="out/realtime_state.json")
    parser.add_argument("--web-state-interval-s", type=float, default=1.0)
    parser.add_argument(
        "--relaxed-planned-tls-demo",
        action="store_true",
        help="Visualization-only fallback: publish nearby TLS candidates when strict corridor planning returns none.",
    )
    parser.add_argument("--relaxed-planned-tls-max", type=int, default=8)
    parser.add_argument("--relaxed-planned-tls-radius-m", type=float, default=2500.0)
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
        min_owner_hold_s=float(controller_profile.get("min_owner_hold_s", cfg.get("min_owner_hold_s", args.min_owner_hold_s))),
        max_owner_hold_s=float(controller_profile.get("max_owner_hold_s", 35.0)),
        min_switch_interval_s=float(controller_profile.get("min_switch_interval_s", 5.0)),
        yellow_transition_s=float(
            controller_profile.get("yellow_transition_s", cfg.get("yellow_transition_s", args.preemption_yellow_s))
        ),
        all_red_s=float(controller_profile.get("all_red_s", cfg.get("all_red_s", args.preemption_all_red_s))),
        post_restore_cooldown_s=float(
            controller_profile.get(
                "post_restore_cooldown_s",
                cfg.get("post_restore_cooldown_s", args.post_restore_cooldown_s),
            )
        ),
        min_dynamic_green_s=float(
            controller_profile.get("min_dynamic_green_s", cfg.get("min_dynamic_green_s", args.min_dynamic_green_s))
        ),
        max_dynamic_green_s=float(
            controller_profile.get("max_dynamic_green_s", cfg.get("max_dynamic_green_s", args.max_dynamic_green_s))
        ),
        queue_gain_s=float(controller_profile.get("queue_gain_s", cfg.get("queue_gain_s", args.queue_gain_s))),
    )

    hospital_edge_map = cfg.get("hospital_edge_map", {})
    hospitals = HospitalRegistry(args.hospitals_csv)
    hospital_points = [
        {"id": h.hospital_id, "name": h.name, "lat": h.lat, "lon": h.lon}
        for h in hospitals.hospitals
    ]
    hospital_by_id = {h.hospital_id: h for h in hospitals.hospitals}
    hospital_name_by_id = {h.hospital_id: h.name for h in hospitals.hospitals}
    net_router = StaticGraphRouter.from_net_file(args.routing_net_file)
    static_router = net_router if args.routing_algorithm in {"dijkstra", "astar"} else None

    enable_debug_gui = bool(args.ambulance_debug_gui and args.sumo_binary == "sumo-gui")

    sumo_cmd = [
        args.sumo_binary,
        "-c",
        args.sumocfg,
        "--start",
        "--collision.action",
        "warn",
        "--time-to-teleport",
        "60",
        "--time-to-teleport.highways",
        "30",
        "--log",
        "out/sumo_runtime.log",
    ]
    if args.sumo_binary == "sumo-gui":
        # Keep GUI readable and prevent instant-close behavior in interactive runs.
        sumo_cmd += ["--delay", "60"]
    else:
        sumo_cmd += ["--quit-on-end"]
    traci.start(sumo_cmd)
    tls_edge_index = build_tls_incoming_edge_index(traci)
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
    assignment_ts_by_vehicle: dict[str, float] = {}
    hospital_load_by_id: dict[str, int] = {}
    hospital_stop_slots: dict[str, int] = {}
    route_fail_retry_after_ts_by_vehicle: dict[str, float] = {}
    dispatch_ts_by_vehicle: dict[str, float] = {}
    last_police_notify_ts_by_vehicle: dict[str, float] = {}
    vehicle_mission_by_id: dict[str, dict] = {}
    vehicle_home_hospital_by_id: dict[str, str] = {}
    driver_details_by_vehicle: dict[str, dict] = {}
    open_calls_by_id: dict[str, dict] = {}
    call_events: list[dict] = []
    call_line_offset = 0
    trip_command_line_offset = 0
    control_command_line_offset = 0
    last_call_dispatch_ts = -1e9
    reached_logged_by_vehicle: set[str] = set()
    breakdown_logged_by_vehicle: set[str] = set()
    stopped_since_by_vehicle: dict[str, float] = {}
    arrival_events: list[dict] = []
    breakdown_events: list[dict] = []
    police_events: list[dict] = []
    hospital_stop_set_by_vehicle: set[str] = set()
    runtime_configured_vehicles: set[str] = set()
    gui_camera_state: dict[str, object] = {"idx": 0, "last_switch_ts": -1e9, "focus_vehicle": "", "view_id": ""}
    gui_tls_marker_state: dict[str, object] = {"shown_tls": []}
    trigger_info: dict = {"confidence": 0.0, "confirmed": False, "sources": [], "matched_lora_events": 0}
    live_feed_info: dict = {
        "traffic_edges": 0,
        "traffic_age_s": None,
        "lora_events": 0,
        "lora_age_s": None,
        "source": "bridged_live",
    }
    selected_corridor_tls: list[str] = []
    planned_corridor_tls: list[str] = []
    planned_corridor_mode = "strict"
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

        sim_time = float(traci.simulation.getTime())

        active_vehicles = resolve_vehicle_ids(traci, manual_ids, args.auto_detect_emergency_vehicles)
        if not active_vehicles:
            controller.restore_finished_tls(traci)

        planned_corridor_tls = extract_planned_corridor_tls(
            traci,
            vehicle_ids=active_vehicles,
            tls_edge_index=tls_edge_index,
            max_tls=40,
        )
        planned_corridor_mode = "strict"
        if (
            not planned_corridor_tls
            and bool(args.relaxed_planned_tls_demo)
            and bool(active_vehicles)
        ):
            planned_corridor_tls = extract_relaxed_demo_planned_tls(
                traci,
                vehicle_ids=active_vehicles,
                max_tls=max(1, int(args.relaxed_planned_tls_max)),
                radius_m=float(args.relaxed_planned_tls_radius_m),
            )
            if planned_corridor_tls:
                planned_corridor_mode = "relaxed_demo"

        for v in active_vehicles:
            colorize_vehicle(traci, v)
            if v not in runtime_configured_vehicles:
                configure_emergency_vehicle_runtime(traci, v)
                runtime_configured_vehicles.add(v)
            if v not in vehicle_home_hospital_by_id:
                try:
                    home_hospital_id = str(traci.vehicle.getParameter(v, "home.hospital")).strip()
                except Exception:
                    home_hospital_id = ""
                vehicle_home_hospital_by_id[v] = home_hospital_id
            if v not in driver_details_by_vehicle:
                driver_details_by_vehicle[v] = build_driver_profile(v)

        new_calls, call_line_offset = read_new_call_requests(args.call_requests_file, call_line_offset)
        for call in new_calls:
            call_id = str(call.get("call_id", "")).strip() or f"call_{int(time.time() * 1000)}"
            if call_id in open_calls_by_id:
                continue
            try:
                lat = float(call.get("lat"))
                lon = float(call.get("lon"))
            except (TypeError, ValueError):
                continue

            matched_edge = map_match_call_to_edge(
                traci,
                lat=lat,
                lon=lon,
                edge_center_xy=net_router.edge_center_xy,
            )
            if not matched_edge:
                continue

            emergency_type = str(call.get("emergency_type", args.emergency_type)).strip().lower()
            if emergency_type not in ALLOWED_HEALTH_EMERGENCY_TYPES:
                print(
                    f"[CALL][SKIP] call_id={call_id} unsupported emergency_type={emergency_type}; "
                    "ambulance corridor supports trauma/cardiac/stroke/general only"
                )
                continue

            open_calls_by_id[call_id] = {
                "call_id": call_id,
                "timestamp": float(call.get("timestamp", time.time())),
                "lat": lat,
                "lon": lon,
                "matched_edge": matched_edge,
                "emergency_type": emergency_type,
                "caller_name": str(call.get("caller_name", "citizen")),
                "preferred_hospital_id": str(call.get("preferred_hospital_id", "")).strip(),
                "status": "pending_dispatch",
                "assigned_vehicle": "",
            }
            call_events.append(
                {
                    "call_id": call_id,
                    "timestamp": round(sim_time, 1),
                    "status": "pending_dispatch",
                    "matched_edge": matched_edge,
                    "lat": lat,
                    "lon": lon,
                    "assigned_vehicle": "",
                }
            )

        new_trip_commands, trip_command_line_offset = read_new_call_requests(
            args.trip_commands_file,
            trip_command_line_offset,
        )
        for cmd in new_trip_commands:
            action = str(cmd.get("action", "")).strip().lower()
            if action != "start_trip":
                continue
            call_id = str(cmd.get("call_id", "")).strip()
            if not call_id:
                continue
            call_events.append(
                {
                    "call_id": call_id,
                    "timestamp": round(sim_time, 1),
                    "status": "start_trip_ignored_auto_dispatch",
                    "assigned_vehicle": str(open_calls_by_id.get(call_id, {}).get("assigned_vehicle", "")),
                }
            )

        control_commands, control_command_line_offset = read_new_call_requests(
            args.control_commands_file,
            control_command_line_offset,
        )
        for cmd in control_commands:
            action = str(cmd.get("action", "")).strip().lower()
            if action not in {"reset_ambulances", "reset_called_ambulances"}:
                continue

            requested_ids = _normalize_vehicle_ids_from_command(cmd)
            reusable_pool = set(reached_logged_by_vehicle) | set(breakdown_logged_by_vehicle)
            if requested_ids:
                reusable_pool &= requested_ids

            if not reusable_pool:
                continue

            for vehicle_id in sorted(reusable_pool):
                reached_logged_by_vehicle.discard(vehicle_id)
                breakdown_logged_by_vehicle.discard(vehicle_id)
                stopped_since_by_vehicle.pop(vehicle_id, None)
                route_fail_retry_after_ts_by_vehicle.pop(vehicle_id, None)
                last_reroute_ts_by_vehicle.pop(vehicle_id, None)
                dispatch_ts_by_vehicle.pop(vehicle_id, None)
                last_police_notify_ts_by_vehicle.pop(vehicle_id, None)
                notified_hospital_id_by_vehicle.pop(vehicle_id, None)
                hospital_stop_set_by_vehicle.discard(vehicle_id)
                selected_plan_by_vehicle.pop(vehicle_id, None)

                hid = assigned_hospital_id_by_vehicle.pop(vehicle_id, None)
                assignment_ts_by_vehicle.pop(vehicle_id, None)
                if hid:
                    hospital_load_by_id[hid] = max(0, int(hospital_load_by_id.get(hid, 1)) - 1)

                mission = vehicle_mission_by_id.pop(vehicle_id, None)
                if mission:
                    call_id = str(mission.get("call_id", "")).strip()
                    if call_id and call_id in open_calls_by_id:
                        open_calls_by_id[call_id]["status"] = "reset_for_redispatch"
                        call_events.append(
                            {
                                "call_id": call_id,
                                "timestamp": round(sim_time, 1),
                                "status": "reset_for_redispatch",
                                "assigned_vehicle": vehicle_id,
                                "lat": float(open_calls_by_id[call_id].get("lat", 0.0)),
                                "lon": float(open_calls_by_id[call_id].get("lon", 0.0)),
                                "matched_edge": str(open_calls_by_id[call_id].get("matched_edge", "")),
                            }
                        )

                try:
                    traci.vehicle.resume(vehicle_id)
                except Exception:
                    pass

            print(f"[RESET] Ambulance pool reset count={len(reusable_pool)}")

        if (sim_time - last_call_dispatch_ts) >= float(args.dispatch_cooldown_s):
            pending_calls = [c for c in open_calls_by_id.values() if c.get("status") == "pending_dispatch"]
            pending_calls.sort(key=lambda x: float(x.get("timestamp", 0.0)))
            idle_vehicles = [
                v
                for v in active_vehicles
                if v not in breakdown_logged_by_vehicle
                and v not in reached_logged_by_vehicle
                and v not in vehicle_mission_by_id
            ]

            for call in pending_calls:
                if not idle_vehicles:
                    break
                incident_edge = str(call.get("matched_edge", ""))
                if not incident_edge:
                    continue

                preferred_hospital_id = str(call.get("preferred_hospital_id", "")).strip()
                dispatch_hospital_id = ""
                if preferred_hospital_id and preferred_hospital_id in hospital_edge_map:
                    dispatch_hospital_id = preferred_hospital_id
                else:
                    call_lat = float(call.get("lat", 0.0))
                    call_lon = float(call.get("lon", 0.0))
                    nearest_hospital_id = ""
                    nearest_dist = float("inf")
                    for hp in hospital_points:
                        try:
                            d = geo_distance_m(call_lat, call_lon, float(hp.get("lat", 0.0)), float(hp.get("lon", 0.0)))
                        except Exception:
                            d = float("inf")
                        if d < nearest_dist:
                            nearest_dist = d
                            nearest_hospital_id = str(hp.get("id", "")).strip()
                    if nearest_hospital_id and nearest_hospital_id in hospital_edge_map:
                        dispatch_hospital_id = nearest_hospital_id

                candidate_vehicles = idle_vehicles
                if dispatch_hospital_id:
                    home_candidates = [
                        v for v in idle_vehicles if str(vehicle_home_hospital_by_id.get(v, "")).strip() == dispatch_hospital_id
                    ]
                    if home_candidates:
                        candidate_vehicles = home_candidates

                best_vehicle = ""
                best_eta = float("inf")
                for vehicle_id in candidate_vehicles:
                    try:
                        start_edge = traci.vehicle.getRoadID(vehicle_id)
                        if (not start_edge or str(start_edge).startswith(":")) and str(
                            vehicle_home_hospital_by_id.get(vehicle_id, "")
                        ).strip() in hospital_edge_map:
                            home_hid = str(vehicle_home_hospital_by_id.get(vehicle_id, "")).strip()
                            start_edge = str(hospital_edge_map.get(home_hid, start_edge))
                        route = traci.simulation.findRoute(start_edge, incident_edge, vType="emergency")
                        eta = float(route.travelTime)
                    except Exception:
                        eta = float("inf")
                    if eta < best_eta:
                        best_eta = eta
                        best_vehicle = vehicle_id

                if not best_vehicle:
                    continue

                plan = build_route_to_hospital(
                    traci,
                    best_vehicle,
                    incident_edge,
                    call.get("call_id", "call"),
                    routing_algorithm=args.routing_algorithm,
                    router=static_router,
                )
                if plan and apply_vehicle_target(
                    traci,
                    best_vehicle,
                    target_edge=incident_edge,
                    fallback_route_edges=plan.route_edges,
                ):
                    try:
                        traci.vehicle.resume(best_vehicle)
                    except Exception:
                        pass
                    vehicle_mission_by_id[best_vehicle] = {
                        "phase": "to_incident",
                        "call_id": str(call.get("call_id", "")),
                        "incident_edge": incident_edge,
                        "emergency_type": str(call.get("emergency_type", args.emergency_type)),
                        "preferred_hospital_id": str(call.get("preferred_hospital_id", "")).strip(),
                        "home_hospital_id": str(vehicle_home_hospital_by_id.get(best_vehicle, "")).strip(),
                        "return_hospital_id": str(dispatch_hospital_id or vehicle_home_hospital_by_id.get(best_vehicle, "")).strip(),
                    }
                    driver = driver_details_by_vehicle.get(best_vehicle, {})
                    selected_plan_by_vehicle[best_vehicle] = {
                        "hospital_id": "",
                        "hospital_name": "Caller Location",
                        "eta_seconds": plan.eta_seconds,
                        "reroute_reason": "dispatch_from_hospital_pool",
                        "route_edges": list(plan.route_edges or []),
                        "police_notification_status": "pending",
                        "driver_name": str(driver.get("driver_name", "")),
                        "driver_phone": str(driver.get("driver_phone", "")),
                    }
                    call["status"] = "assigned"
                    call["assigned_vehicle"] = best_vehicle
                    call["assigned_hospital_id"] = str(dispatch_hospital_id or vehicle_home_hospital_by_id.get(best_vehicle, "")).strip()
                    call["assigned_hospital_name"] = str(
                        hospital_name_by_id.get(call.get("assigned_hospital_id", ""), call.get("assigned_hospital_id", ""))
                    )
                    call["driver_name"] = str(driver.get("driver_name", ""))
                    call["driver_phone"] = str(driver.get("driver_phone", ""))
                    call_events.append(
                        {
                            "call_id": str(call.get("call_id", "")),
                            "timestamp": round(sim_time, 1),
                            "status": "assigned",
                            "matched_edge": incident_edge,
                            "lat": float(call.get("lat", 0.0)),
                            "lon": float(call.get("lon", 0.0)),
                            "assigned_vehicle": best_vehicle,
                            "assigned_hospital_id": str(call.get("assigned_hospital_id", "")),
                        }
                    )
                    idle_vehicles = [v for v in idle_vehicles if v != best_vehicle]

            last_call_dispatch_ts = sim_time

        update_breakdown_status(
            traci=traci,
            sim_time=sim_time,
            active_vehicles=active_vehicles,
            reached_logged_by_vehicle=reached_logged_by_vehicle,
            breakdown_logged_by_vehicle=breakdown_logged_by_vehicle,
            vehicle_mission_by_id=vehicle_mission_by_id,
            stopped_since_by_vehicle=stopped_since_by_vehicle,
            stop_threshold_s=float(args.breakdown_stop_threshold_s),
            breakdown_events=breakdown_events,
        )

        if enable_debug_gui and active_vehicles:
            display_vehicles = [
                v for v in active_vehicles if v not in reached_logged_by_vehicle and v not in breakdown_logged_by_vehicle
            ]
            if not display_vehicles:
                display_vehicles = active_vehicles
            mission_priority = [
                v
                for v in display_vehicles
                if v in vehicle_mission_by_id
                and str(vehicle_mission_by_id.get(v, {}).get("phase", "")) in {"to_incident", "to_hospital"}
            ]
            update_ambulance_debug_camera(
                traci,
                display_vehicles,
                gui_camera_state,
                switch_interval_s=float(args.camera_switch_interval_s),
                zoom=float(args.camera_zoom),
                hide_non_emergency_labels=bool(args.hide_non_emergency_labels),
                follow_mode=str(args.camera_follow_mode),
                priority_vehicles=mission_priority,
            )

        if steps % 50 == 0 and active_vehicles:
            print(f"[INFO] t={traci.simulation.getTime():.1f}s active_emergency={active_vehicles}")
            probe_candidates = [
                v
                for v in active_vehicles
                if v in vehicle_mission_by_id
                and str(vehicle_mission_by_id.get(v, {}).get("phase", "")) in {"to_incident", "to_hospital"}
            ]
            probe = probe_candidates[0] if probe_candidates else active_vehicles[0]
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
        roadside_obs = load_live_traffic(
            args.live_traffic_file,
            now_ts=wall_now_ts,
            max_age_s=float(args.live_traffic_max_age_s),
        )
        matched_lora_events = map_match_lora_events(
            traci,
            lora_file_events,
            static_router.edge_center_xy if static_router is not None else {},
        )

        latest_lora_ts = max([float(e.get("timestamp", 0.0)) for e in lora_file_events], default=0.0)
        latest_traffic_ts = max([float(v.timestamp) for v in roadside_obs.values()], default=0.0)
        live_feed_info = {
            "traffic_edges": len(roadside_obs),
            "traffic_age_s": None if latest_traffic_ts <= 0 else round(max(0.0, wall_now_ts - latest_traffic_ts), 1),
            "lora_events": len(lora_file_events),
            "lora_age_s": None if latest_lora_ts <= 0 else round(max(0.0, wall_now_ts - latest_lora_ts), 1),
            "source": "bridged_live",
        }

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

        active_dispatch_vehicles = [
            v
            for v in active_vehicles
            if v in vehicle_mission_by_id
            and str(vehicle_mission_by_id.get(v, {}).get("phase", "")) in {"to_incident", "to_hospital"}
            and v not in breakdown_logged_by_vehicle
        ]
        should_preempt = bool(args.force_green_corridor or confirmed or active_dispatch_vehicles)
        if should_preempt:
            now = traci.simulation.getTime()
            live_edge_costs = None
            if static_router is not None:
                sumo_costs = snapshot_live_edge_costs(traci, static_router)
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
                mission = vehicle_mission_by_id.get(vehicle_id)
                vehicle_emergency_type = str(mission.get("emergency_type", args.emergency_type)) if mission else str(args.emergency_type)

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

                if mission and mission.get("phase") == "to_incident":
                    incident_edge = str(mission.get("incident_edge", ""))
                    if not incident_edge:
                        last_reroute_ts_by_vehicle[vehicle_id] = now
                        continue

                    plan = build_route_to_hospital(
                        traci,
                        vehicle_id,
                        incident_edge,
                        str(mission.get("call_id", "call")),
                        routing_algorithm=args.routing_algorithm,
                        router=static_router,
                        edge_cost_override_s=live_edge_costs,
                    )
                    if plan:
                        apply_vehicle_target(
                            traci,
                            vehicle_id,
                            target_edge=incident_edge,
                            fallback_route_edges=plan.route_edges,
                        )
                        try:
                            traci.vehicle.resume(vehicle_id)
                        except Exception:
                            pass
                        selected_plan_by_vehicle.setdefault(vehicle_id, {})["hospital_name"] = "Caller Location"
                        selected_plan_by_vehicle.setdefault(vehicle_id, {})["eta_seconds"] = plan.eta_seconds
                        selected_plan_by_vehicle.setdefault(vehicle_id, {})["reroute_reason"] = "dispatch_to_caller"

                    try:
                        speed_now = float(traci.vehicle.getSpeed(vehicle_id))
                    except Exception:
                        speed_now = 0.0

                    try:
                        dist_to_incident = float(traci.vehicle.getDrivingDistance(vehicle_id, incident_edge, 0.0))
                    except Exception:
                        dist_to_incident = float("inf")

                    arrived_incident = (
                        current_edge == incident_edge
                        or (dist_to_incident >= 0.0 and dist_to_incident <= 25.0)
                    )
                    if arrived_incident and speed_now <= 8.0:
                        call_id = str(mission.get("call_id", ""))
                        if call_id in open_calls_by_id:
                            mission["phase"] = "to_hospital"
                            open_calls_by_id[call_id]["status"] = "picked_up"
                            call_events.append(
                                {
                                    "call_id": call_id,
                                    "timestamp": round(float(now), 1),
                                    "status": "picked_up_auto",
                                    "matched_edge": incident_edge,
                                    "lat": float(open_calls_by_id.get(call_id, {}).get("lat", 0.0)),
                                    "lon": float(open_calls_by_id.get(call_id, {}).get("lon", 0.0)),
                                    "assigned_vehicle": vehicle_id,
                                }
                            )
                    last_reroute_ts_by_vehicle[vehicle_id] = now
                    continue

                if not mission and not bool(args.force_green_corridor or confirmed):
                    last_reroute_ts_by_vehicle[vehicle_id] = now
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
                best = hospitals.select_best(vehicle_emergency_type, eta_adjusted)

                if mission and mission.get("phase") == "to_hospital":
                    suitable_ids = {
                        h.hospital_id
                        for h in hospitals.suitable_hospitals(vehicle_emergency_type)
                        if h.hospital_id in hospital_edge_map
                    }
                    closest_hid = ""
                    closest_eta = float("inf")
                    for hid in suitable_ids:
                        eta_val = float(etas.get(hid, float("inf")))
                        if eta_val < closest_eta:
                            closest_eta = eta_val
                            closest_hid = hid
                    if closest_hid and closest_eta < float("inf"):
                        best = hospital_by_id.get(closest_hid, best)

                assigned_id = assigned_hospital_id_by_vehicle.get(vehicle_id)
                now_ts = float(now)
                if assigned_id and assigned_id in hospital_edge_map:
                    assigned_eta = float(eta_adjusted.get(assigned_id, float("inf")))
                    candidate_eta = float(
                        eta_adjusted.get(getattr(best, "hospital_id", ""), float("inf")) if best is not None else float("inf")
                    )
                    assigned_since = float(assignment_ts_by_vehicle.get(vehicle_id, now_ts))
                    dwell_elapsed = max(0.0, now_ts - assigned_since)
                    min_dwell_s = float(args.reroute_min_dwell_s)
                    eta_gain_needed = float(args.reroute_switch_eta_improvement_s)

                    # Anti-flap policy: keep current assignment during dwell period unless it becomes unreachable.
                    keep_current = False
                    if assigned_eta < float("inf"):
                        if dwell_elapsed < min_dwell_s:
                            keep_current = True
                        elif candidate_eta >= (assigned_eta - eta_gain_needed):
                            keep_current = True

                    if keep_current:
                        best = next((h for h in hospitals.hospitals if h.hospital_id == assigned_id), best)
                if best and best.hospital_id in hospital_edge_map:
                    reroute_reason = "best_eta_capacity"
                    if mission and mission.get("phase") == "to_hospital":
                        return_hospital_id = str(mission.get("return_hospital_id", "")).strip()
                        preferred_hospital_id = str(mission.get("preferred_hospital_id", "")).strip()
                        if return_hospital_id and best.hospital_id == return_hospital_id:
                            reroute_reason = "return_home_hospital"
                        elif preferred_hospital_id and best.hospital_id == preferred_hospital_id:
                            reroute_reason = "preferred_hospital_nearby"
                    if assigned_id == best.hospital_id:
                        reroute_reason = "hysteresis_keep_assigned"
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
                        try:
                            traci.vehicle.resume(vehicle_id)
                        except Exception:
                            pass
                        prev_assigned = assigned_hospital_id_by_vehicle.get(vehicle_id)
                        if prev_assigned and prev_assigned != best.hospital_id:
                            hospital_load_by_id[prev_assigned] = max(0, int(hospital_load_by_id.get(prev_assigned, 1)) - 1)
                        if prev_assigned != best.hospital_id:
                            hospital_load_by_id[best.hospital_id] = int(hospital_load_by_id.get(best.hospital_id, 0)) + 1
                            assignment_ts_by_vehicle[vehicle_id] = float(now)
                        assigned_hospital_id_by_vehicle[vehicle_id] = best.hospital_id
                        if vehicle_id not in dispatch_ts_by_vehicle:
                            dispatch_ts_by_vehicle[vehicle_id] = float(now)
                        selected_plan_by_vehicle[vehicle_id] = {
                            "hospital_id": best.hospital_id,
                            "hospital_name": best.name,
                            "eta_seconds": plan.eta_seconds,
                            "reroute_reason": reroute_reason,
                            "route_edges": list(plan.route_edges or []),
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
                            f"(eta={plan.eta_seconds:.1f}s, type={vehicle_emergency_type})"
                        )
                        if notified_hospital_id_by_vehicle.get(vehicle_id) != best.hospital_id:
                            send_hospital_pre_notification(
                                best,
                                vehicle_id=vehicle_id,
                                emergency_type=vehicle_emergency_type,
                                eta_seconds=plan.eta_seconds,
                                fallback_log_path=args.hospital_log,
                            )
                            notified_hospital_id_by_vehicle[vehicle_id] = best.hospital_id
                    else:
                        # Backoff retries to avoid repeated invalid updates at same junction point.
                        route_fail_retry_after_ts_by_vehicle[vehicle_id] = float(now) + 4.0

                last_reroute_ts_by_vehicle[vehicle_id] = now

            priority_by_vehicle = {
                v: get_priority_score(
                    v,
                    str(vehicle_mission_by_id.get(v, {}).get("emergency_type", args.emergency_type)),
                )
                for v in controllable_vehicles
            }
            controller.preempt_for_vehicles(traci, controllable_vehicles, priority_by_vehicle)
            selected_corridor_tls = sorted(controller.active_tls_ids())
            tls_owner_map = controller.tls_owner_map()

            for vehicle_id in controllable_vehicles:
                plan = selected_plan_by_vehicle.get(vehicle_id, {})
                eta_seconds = plan.get("eta_seconds")
                reroute_reason = str(plan.get("reroute_reason", "best_eta_capacity"))

                corridor_for_vehicle = [
                    tls_id
                    for tls_id, owner in tls_owner_map.items()
                    if owner == vehicle_id
                ]

                last_police_ts = float(last_police_notify_ts_by_vehicle.get(vehicle_id, -1e9))
                if float(now) - last_police_ts < float(args.police_notify_cooldown_s):
                    continue

                police_result = send_police_notification(
                    endpoint=str(args.police_endpoint),
                    sms_to=str(args.police_sms_to),
                    fallback_log_path=str(args.police_log),
                    vehicle_id=vehicle_id,
                    emergency_type=str(vehicle_mission_by_id.get(vehicle_id, {}).get("emergency_type", args.emergency_type)),
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
            tls_owner_map = controller.tls_owner_map()

        if enable_debug_gui:
            sync_preempted_tls_gui_markers(
                traci,
                selected_corridor_tls,
                tls_owner_map,
                gui_tls_marker_state,
            )

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
            assignment_ts_by_vehicle.pop(vehicle_id, None)
            if hid:
                hospital_load_by_id[hid] = max(0, int(hospital_load_by_id.get(hid, 1)) - 1)
            mission = vehicle_mission_by_id.pop(vehicle_id, None)
            if mission:
                call_id = str(mission.get("call_id", ""))
                if call_id in open_calls_by_id:
                    status = "closed_reached" if vehicle_id in reached_logged_by_vehicle else "closed_breakdown"
                    call_info = dict(open_calls_by_id.get(call_id, {}))
                    call_info["status"] = status
                    open_calls_by_id[call_id]["status"] = status
                    call_events.append(
                        {
                            "call_id": call_id,
                            "timestamp": round(sim_time, 1),
                            "status": status,
                            "assigned_vehicle": vehicle_id,
                            "lat": float(call_info.get("lat", 0.0)),
                            "lon": float(call_info.get("lon", 0.0)),
                            "matched_edge": str(call_info.get("matched_edge", "")),
                        }
                    )
                    if status == "closed_reached":
                        open_calls_by_id.pop(call_id, None)
        if args.status_panel_log and (sim_time - last_panel_ts) >= float(args.status_panel_interval_s):
            focus_vehicle = str(gui_camera_state.get("focus_vehicle", ""))
            preferred = [v for v in active_vehicles if v not in reached_logged_by_vehicle and v not in breakdown_logged_by_vehicle]
            if not preferred:
                preferred = active_vehicles
            if not preferred:
                last_panel_ts = sim_time
                continue
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
            # Keep signals visible in UI even when no active preemption is currently held.
            tls_snapshot = build_tls_snapshot(traci, controller, only_preempted=False)
            write_web_state(
                traci=traci,
                sim_time=sim_time,
                active_vehicles=active_vehicles,
                selected_plan_by_vehicle=selected_plan_by_vehicle,
                vehicle_mission_by_id=vehicle_mission_by_id,
                driver_details_by_vehicle=driver_details_by_vehicle,
                vehicle_home_hospital_by_id=vehicle_home_hospital_by_id,
                hospital_name_by_id=hospital_name_by_id,
                reached_logged_by_vehicle=reached_logged_by_vehicle,
                breakdown_logged_by_vehicle=breakdown_logged_by_vehicle,
                arrival_events=arrival_events,
                breakdown_events=breakdown_events,
                tls_snapshot=tls_snapshot,
                active_tls_preempted_count=controller.active_tls_count(),
                hospitals=hospital_points,
                trigger_info=trigger_info,
                live_feed_info=live_feed_info,
                selected_corridor_tls=selected_corridor_tls,
                planned_corridor_tls=planned_corridor_tls,
                planned_corridor_mode=planned_corridor_mode,
                police_events=police_events,
                call_events=call_events,
                call_markers=list(open_calls_by_id.values()),
                edge_center_xy=net_router.edge_center_xy,
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
            if enable_debug_gui:
                sync_preempted_tls_gui_markers(traci, [], {}, gui_tls_marker_state)
        except Exception:
            pass
        try:
            traci.close()
        except Exception:
            pass
    else:
        print("[WARN] SUMO disconnected early. Check out/sumo_runtime.log for root cause.")
    print("[INFO] Simulation ended")


if __name__ == "__main__":
    main()
