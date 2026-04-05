class GreenCorridorController:
    def __init__(
        self,
        preemption_phases: dict[str, int],
        lookahead_m: float = 350.0,
        hold_green_s: float = 18.0,
        restore_after_s: float = 20.0,
        max_owner_hold_s: float = 35.0,
        min_switch_interval_s: float = 5.0,
    ) -> None:
        self.preemption_phases = preemption_phases
        self.lookahead_m = float(lookahead_m)
        self.hold_green_s = float(hold_green_s)
        self.restore_after_s = float(restore_after_s)
        self.max_owner_hold_s = float(max_owner_hold_s)
        self.min_switch_interval_s = float(min_switch_interval_s)

        self._baseline: dict[str, tuple[str, int, float]] = {}
        self._active_ts: dict[str, float] = {}
        self._tls_owner: dict[str, str] = {}
        self._tls_owner_since: dict[str, float] = {}
        self._last_switch_ts: dict[str, float] = {}

    @staticmethod
    def _is_green(state_char: str) -> bool:
        return state_char in {"g", "G"}

    def _find_phase_for_link(self, traci, tls_id: str, link_index: int) -> int | None:
        """Return a phase index that gives green to the given link if one exists."""
        try:
            logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
        except Exception:
            return None

        for i, phase in enumerate(logic.phases):
            state = phase.state
            if 0 <= link_index < len(state) and self._is_green(state[link_index]):
                return i

        return None

    def _store_baseline(self, traci, tls_id: str) -> None:
        if tls_id in self._baseline:
            return
        self._baseline[tls_id] = (
            traci.trafficlight.getProgram(tls_id),
            traci.trafficlight.getPhase(tls_id),
            traci.trafficlight.getPhaseDuration(tls_id),
        )

    def _activate(self, traci, tls_id: str, link_index: int, sim_time: float) -> None:
        self._store_baseline(traci, tls_id)
        desired_phase = self.preemption_phases.get(tls_id)
        if desired_phase is None:
            desired_phase = self._find_phase_for_link(traci, tls_id, link_index)
        if desired_phase is None:
            return

        current_phase = traci.trafficlight.getPhase(tls_id)
        if current_phase != desired_phase:
            traci.trafficlight.setPhase(tls_id, desired_phase)
            self._last_switch_ts[tls_id] = sim_time
            owner = self._tls_owner.get(tls_id, "unknown")
            print(
                f"[SIGNAL] t={sim_time:.1f}s tls={tls_id} owner={owner} "
                f"phase {current_phase}->{desired_phase} (link={link_index})"
            )
        traci.trafficlight.setPhaseDuration(tls_id, self.hold_green_s)
        self._active_ts[tls_id] = sim_time

    def preempt_for_vehicle(self, traci, vehicle_id: str) -> None:
        self.preempt_for_vehicles(traci, [vehicle_id], {vehicle_id: 1.0})

    def _can_switch_owner(self, tls_id: str, sim_time: float, new_owner: str) -> bool:
        current_owner = self._tls_owner.get(tls_id)
        if not current_owner or current_owner == new_owner:
            return True

        owner_since = self._tls_owner_since.get(tls_id, sim_time)
        last_switch = self._last_switch_ts.get(tls_id, -1e9)

        # Avoid rapid oscillation and provide fairness before ownership changes.
        if sim_time - last_switch < self.min_switch_interval_s:
            return False
        if sim_time - owner_since < self.max_owner_hold_s:
            return False

        return True

    def preempt_for_vehicles(self, traci, vehicle_ids: list[str], priority_by_vehicle: dict[str, float]) -> None:
        sim_time = traci.simulation.getTime()
        requests: dict[str, tuple[str, int, float]] = {}

        for vehicle_id in vehicle_ids:
            if vehicle_id not in traci.vehicle.getIDList():
                continue
            next_tls = traci.vehicle.getNextTLS(vehicle_id)

            for tls_id, link_idx, distance, _state in next_tls:
                if distance > self.lookahead_m:
                    continue

                # Higher score wins: closer ambulance and higher priority.
                vehicle_priority = float(priority_by_vehicle.get(vehicle_id, 1.0))
                score = vehicle_priority * 1000.0 - float(distance)

                current = requests.get(tls_id)
                if current is None or score > current[2]:
                    requests[tls_id] = (vehicle_id, link_idx, score)

        for tls_id, (vehicle_id, link_idx, _score) in requests.items():
            if not self._can_switch_owner(tls_id, sim_time, vehicle_id):
                continue
            self._tls_owner[tls_id] = vehicle_id
            self._tls_owner_since[tls_id] = sim_time
            self._activate(traci, tls_id, link_idx, sim_time)

        if vehicle_ids and not requests:
            print(f"[SIGNAL] t={sim_time:.1f}s no preemption candidates within lookahead={self.lookahead_m:.1f}m")

    def restore_finished_tls(self, traci, force_all: bool = False) -> None:
        sim_time = traci.simulation.getTime()
        restore_ids = []
        for tls_id, last_seen in self._active_ts.items():
            if force_all or (sim_time - last_seen >= self.restore_after_s):
                restore_ids.append(tls_id)

        for tls_id in restore_ids:
            baseline = self._baseline.get(tls_id)
            if baseline:
                program_id, phase, phase_duration = baseline
                try:
                    traci.trafficlight.setProgram(tls_id, program_id)
                    traci.trafficlight.setPhase(tls_id, phase)
                    traci.trafficlight.setPhaseDuration(tls_id, phase_duration)
                    print(f"[SIGNAL] t={sim_time:.1f}s tls={tls_id} restored program={program_id} phase={phase}")
                except Exception:
                    pass
            self._active_ts.pop(tls_id, None)
            self._baseline.pop(tls_id, None)
            self._tls_owner.pop(tls_id, None)
            self._tls_owner_since.pop(tls_id, None)

    def active_tls_count(self) -> int:
        return len(self._active_ts)

    def active_tls_ids(self) -> list[str]:
        return list(self._active_ts.keys())

    def tls_owner_map(self) -> dict[str, str]:
        return dict(self._tls_owner)
