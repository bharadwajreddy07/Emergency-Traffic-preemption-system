from dataclasses import dataclass
import heapq
import math
import xml.etree.ElementTree as ET


@dataclass
class RoutePlan:
    hospital_id: str
    eta_seconds: float
    route_edges: list[str]


class StaticGraphRouter:
    """Edge-graph shortest path router using net XML topology."""

    def __init__(
        self,
        adjacency: dict[str, list[str]],
        edge_cost_s: dict[str, float],
        edge_center_xy: dict[str, tuple[float, float]],
    ) -> None:
        self.adjacency = adjacency
        self.edge_cost_s = edge_cost_s
        self.edge_center_xy = edge_center_xy

    @classmethod
    def from_net_file(cls, net_file: str) -> "StaticGraphRouter":
        tree = ET.parse(net_file)
        root = tree.getroot()

        edge_cost_s: dict[str, float] = {}
        edge_center_xy: dict[str, tuple[float, float]] = {}

        for edge in root.findall("edge"):
            edge_id = edge.attrib.get("id", "")
            if not edge_id or edge_id.startswith(":"):
                continue

            lane_lengths = []
            lane_speeds = []
            for lane in edge.findall("lane"):
                try:
                    lane_lengths.append(float(lane.attrib.get("length", "0") or 0.0))
                except ValueError:
                    pass
                try:
                    lane_speeds.append(float(lane.attrib.get("speed", "13.9") or 13.9))
                except ValueError:
                    pass

                shape = lane.attrib.get("shape", "").strip()
                if shape and edge_id not in edge_center_xy:
                    pts = []
                    for part in shape.split():
                        if "," not in part:
                            continue
                        try:
                            x_str, y_str = part.split(",", 1)
                            pts.append((float(x_str), float(y_str)))
                        except ValueError:
                            continue
                    if pts:
                        first = pts[0]
                        last = pts[-1]
                        edge_center_xy[edge_id] = ((first[0] + last[0]) * 0.5, (first[1] + last[1]) * 0.5)

            length_m = max(lane_lengths) if lane_lengths else 30.0
            speed_mps = max(lane_speeds) if lane_speeds else 13.9
            edge_cost_s[edge_id] = max(0.5, length_m / max(0.1, speed_mps))

        adjacency: dict[str, list[str]] = {edge_id: [] for edge_id in edge_cost_s}
        for conn in root.findall("connection"):
            from_edge = conn.attrib.get("from", "")
            to_edge = conn.attrib.get("to", "")
            if from_edge in adjacency and to_edge in edge_cost_s and not to_edge.startswith(":"):
                adjacency[from_edge].append(to_edge)

        return cls(adjacency=adjacency, edge_cost_s=edge_cost_s, edge_center_xy=edge_center_xy)

    def _heuristic(self, edge_id: str, goal_edge: str) -> float:
        a = self.edge_center_xy.get(edge_id)
        b = self.edge_center_xy.get(goal_edge)
        if not a or not b:
            return 0.0
        dist_m = math.hypot(a[0] - b[0], a[1] - b[1])
        # Optimistic speed upper bound to keep A* admissible.
        return dist_m / 40.0

    def shortest_path(
        self,
        start_edge: str,
        goal_edge: str,
        algorithm: str,
        edge_cost_override_s: dict[str, float] | None = None,
    ) -> tuple[list[str], float]:
        if start_edge not in self.adjacency or goal_edge not in self.adjacency:
            return [], float("inf")

        cost_map = edge_cost_override_s or self.edge_cost_s

        dist: dict[str, float] = {start_edge: 0.0}
        prev: dict[str, str] = {}
        frontier: list[tuple[float, str]] = [(0.0, start_edge)]
        visited: set[str] = set()

        while frontier:
            priority, edge_id = heapq.heappop(frontier)
            if edge_id in visited:
                continue
            visited.add(edge_id)
            if edge_id == goal_edge:
                break

            g_cost = dist.get(edge_id, float("inf"))
            for nxt in self.adjacency.get(edge_id, []):
                cand = g_cost + cost_map.get(nxt, self.edge_cost_s.get(nxt, 1.0))
                if cand < dist.get(nxt, float("inf")):
                    dist[nxt] = cand
                    prev[nxt] = edge_id
                    if algorithm == "astar":
                        heapq.heappush(frontier, (cand + self._heuristic(nxt, goal_edge), nxt))
                    else:
                        heapq.heappush(frontier, (cand, nxt))

        if goal_edge not in dist:
            return [], float("inf")

        route = [goal_edge]
        cur = goal_edge
        while cur in prev:
            cur = prev[cur]
            route.append(cur)
        route.reverse()
        return route, float(dist[goal_edge])


def snapshot_live_edge_costs(
    traci,
    router: StaticGraphRouter,
    min_edge_time_s: float = 0.5,
    max_edge_time_s: float = 300.0,
) -> dict[str, float]:
    """Capture live per-edge travel-time costs from SUMO with static fallback."""
    live_costs: dict[str, float] = {}
    for edge_id, static_cost in router.edge_cost_s.items():
        try:
            t_s = float(traci.edge.getTraveltime(edge_id))
        except Exception:
            t_s = -1.0

        if not math.isfinite(t_s) or t_s <= 0.0:
            try:
                avg_speed = float(traci.edge.getLastStepMeanSpeed(edge_id))
                if avg_speed > 0.1:
                    # Convert speed observation to a robust travel-time estimate.
                    fallback_len_m = max(5.0, static_cost * 13.9)
                    t_s = fallback_len_m / avg_speed
            except Exception:
                t_s = -1.0

        if not math.isfinite(t_s) or t_s <= 0.0:
            t_s = static_cost

        live_costs[edge_id] = max(min_edge_time_s, min(max_edge_time_s, t_s))

    return live_costs


def _resolve_current_edge(traci, vehicle_id: str, router: StaticGraphRouter | None) -> str:
    current_edge = traci.vehicle.getRoadID(vehicle_id)
    if current_edge and not current_edge.startswith(":"):
        return current_edge

    route_edges = traci.vehicle.getRoute(vehicle_id)
    route_index = traci.vehicle.getRouteIndex(vehicle_id)
    for idx in range(max(0, route_index), len(route_edges)):
        candidate = route_edges[idx]
        if candidate.startswith(":"):
            continue
        if router is None or candidate in router.adjacency:
            return candidate

    return current_edge


def estimate_etas_for_hospitals(
    traci,
    vehicle_id: str,
    hospital_edge_map: dict[str, str],
    routing_algorithm: str = "sumo",
    router: StaticGraphRouter | None = None,
    edge_cost_override_s: dict[str, float] | None = None,
) -> dict[str, float]:
    etas: dict[str, float] = {}
    current_edge = _resolve_current_edge(traci, vehicle_id, router)

    for hospital_id, target_edge in hospital_edge_map.items():
        try:
            if routing_algorithm in {"dijkstra", "astar"} and router is not None:
                _route_edges, eta_s = router.shortest_path(
                    current_edge,
                    target_edge,
                    routing_algorithm,
                    edge_cost_override_s=edge_cost_override_s,
                )
                etas[hospital_id] = float(eta_s)
            else:
                route = traci.simulation.findRoute(current_edge, target_edge, vType="emergency")
                etas[hospital_id] = float(route.travelTime)
        except Exception:
            etas[hospital_id] = float("inf")

    return etas


def build_route_to_hospital(
    traci,
    vehicle_id: str,
    target_edge: str,
    hospital_id: str,
    routing_algorithm: str = "sumo",
    router: StaticGraphRouter | None = None,
    edge_cost_override_s: dict[str, float] | None = None,
) -> RoutePlan | None:
    current_edge = _resolve_current_edge(traci, vehicle_id, router)
    try:
        if routing_algorithm in {"dijkstra", "astar"} and router is not None:
            edges, eta_s = router.shortest_path(
                current_edge,
                target_edge,
                routing_algorithm,
                edge_cost_override_s=edge_cost_override_s,
            )
            if not edges:
                return None
            return RoutePlan(hospital_id=hospital_id, eta_seconds=float(eta_s), route_edges=list(edges))

        route = traci.simulation.findRoute(current_edge, target_edge, vType="emergency")
        if not route.edges:
            return None
        return RoutePlan(hospital_id=hospital_id, eta_seconds=float(route.travelTime), route_edges=list(route.edges))
    except Exception:
        return None


def apply_vehicle_route(traci, vehicle_id: str, route_edges: list[str]) -> bool:
    if not route_edges:
        return False
    try:
        traci.vehicle.setRoute(vehicle_id, route_edges)
        return True
    except Exception:
        return False


def apply_vehicle_target(traci, vehicle_id: str, target_edge: str, fallback_route_edges: list[str] | None = None) -> bool:
    """Prefer SUMO-native target change to avoid invalid edge-to-edge replacements."""
    try:
        traci.vehicle.changeTarget(vehicle_id, target_edge)
        return True
    except Exception:
        pass

    if fallback_route_edges:
        try:
            traci.vehicle.setRoute(vehicle_id, fallback_route_edges)
            return True
        except Exception:
            return False

    return False
