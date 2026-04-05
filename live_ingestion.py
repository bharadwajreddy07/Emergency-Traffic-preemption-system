import json
import math
import os
import time
from dataclasses import dataclass


@dataclass
class LiveTrafficObservation:
    edge_id: str
    speed_kmh: float
    occupancy: float
    confidence: float
    is_incident: bool
    timestamp: float


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_live_traffic(path: str, now_ts: float, max_age_s: float) -> dict[str, LiveTrafficObservation]:
    """Read edge-level traffic observations from a JSON file.

    Expected JSON shape:
    {
      "timestamp": 1710000000,
      "edges": [
        {"edge_id": "123", "speed_kmh": 18.0, "occupancy": 0.65, "confidence": 0.9, "incident": false}
      ]
    }
    """
    if not path or not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    rows = payload.get("edges", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}

    observations: dict[str, LiveTrafficObservation] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        edge_id = str(row.get("edge_id", "")).strip()
        if not edge_id or edge_id.startswith(":"):
            continue

        ts = _to_float(row.get("timestamp", payload.get("timestamp", now_ts)), default=now_ts)
        if max_age_s > 0 and (now_ts - ts) > max_age_s:
            continue

        obs = LiveTrafficObservation(
            edge_id=edge_id,
            speed_kmh=max(0.0, _to_float(row.get("speed_kmh", 0.0))),
            occupancy=_clamp(_to_float(row.get("occupancy", 0.0)), 0.0, 1.0),
            confidence=_clamp(_to_float(row.get("confidence", 0.7)), 0.0, 1.0),
            is_incident=bool(row.get("incident", False)),
            timestamp=ts,
        )
        observations[edge_id] = obs

    return observations


def merge_routing_costs(
    base_costs: dict[str, float],
    traffic_by_edge: dict[str, LiveTrafficObservation],
    base_weight: float,
    live_weight: float,
    incident_penalty_s: float,
) -> dict[str, float]:
    """Blend SUMO travel-times with roadside traffic observations for routing."""
    if not base_costs:
        return {}

    base_weight = max(0.0, float(base_weight))
    live_weight = max(0.0, float(live_weight))
    if base_weight == 0.0 and live_weight == 0.0:
        base_weight = 1.0

    merged: dict[str, float] = {}
    ref_speed_kmh = 35.0

    for edge_id, base_cost in base_costs.items():
        base_t = max(0.5, float(base_cost))
        obs = traffic_by_edge.get(edge_id)
        if obs is None:
            merged[edge_id] = base_t
            continue

        speed_penalty = 0.0
        if obs.speed_kmh > 0.0:
            speed_penalty = max(0.0, (ref_speed_kmh - obs.speed_kmh) / ref_speed_kmh)

        live_factor = 1.0 + (obs.occupancy * 1.8) + (speed_penalty * 1.2)
        live_t = base_t * max(0.4, live_factor)
        if obs.is_incident:
            live_t += max(0.0, float(incident_penalty_s))

        # Confidence attenuates how strongly live observations affect the edge cost.
        confidence_weight = _clamp(obs.confidence, 0.0, 1.0)
        weighted_live = live_t * confidence_weight + base_t * (1.0 - confidence_weight)

        denom = base_weight + live_weight
        if denom <= 0.0:
            merged_t = base_t
        else:
            merged_t = ((base_t * base_weight) + (weighted_live * live_weight)) / denom

        if not math.isfinite(merged_t) or merged_t <= 0.0:
            merged_t = base_t

        merged[edge_id] = max(0.5, min(600.0, merged_t))

    return merged


def load_lora_events(path: str, now_ts: float, max_age_s: float) -> list[dict]:
    """Load LoRa events from JSON or JSONL.

    JSON format:
      {"events": [{...}]}
    JSONL format:
      one JSON object per line
    """
    if not path or not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return []

    if not raw:
        return []

    rows: list[dict] = []
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            maybe_rows = payload.get("events", [])
            if isinstance(maybe_rows, list):
                rows = [r for r in maybe_rows if isinstance(r, dict)]
        elif isinstance(payload, list):
            rows = [r for r in payload if isinstance(r, dict)]
    except json.JSONDecodeError:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)

    filtered: list[dict] = []
    for row in rows:
        ts = _to_float(row.get("timestamp", now_ts), now_ts)
        if max_age_s > 0 and (now_ts - ts) > max_age_s:
            continue
        row = dict(row)
        row["timestamp"] = ts
        filtered.append(row)

    return filtered


def map_match_lora_events(traci, events: list[dict], edge_center_xy: dict[str, tuple[float, float]]) -> list[dict]:
    """Map-match LoRa GPS points to nearest SUMO edge center.

    Event expects lat/lon and optionally ambulance_id.
    """
    if not events or not edge_center_xy:
        return []

    matched: list[dict] = []
    centers = list(edge_center_xy.items())

    for event in events:
        if not isinstance(event, dict):
            continue

        lat = event.get("lat")
        lon = event.get("lon")
        if lat is None or lon is None:
            continue

        try:
            x, y = traci.simulation.convertGeo(float(lon), float(lat), True)
        except Exception:
            continue

        best_edge = ""
        best_dist = float("inf")
        for edge_id, (cx, cy) in centers:
            d2 = (float(cx) - float(x)) ** 2 + (float(cy) - float(y)) ** 2
            if d2 < best_dist:
                best_dist = d2
                best_edge = edge_id

        if not best_edge:
            continue

        row = dict(event)
        row["matched_edge"] = best_edge
        row["match_distance_m"] = round(math.sqrt(best_dist), 2)
        matched.append(row)

    return matched


def derive_trigger_confidence(siren_score: float, wireless_seen: bool, geo_valid: bool) -> float:
    score = max(0.0, min(1.0, float(siren_score)))
    wireless_component = 1.0 if wireless_seen else 0.0
    geo_component = 1.0 if geo_valid else 0.0
    confidence = (score * 0.55) + (wireless_component * 0.30) + (geo_component * 0.15)
    return max(0.0, min(1.0, confidence))
