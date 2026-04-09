class GreenCorridorController:
    def __init__(
        self,
        preemption_phases: dict[str, int],
        lookahead_m: float = 350.0,
        hold_green_s: float = 18.0,
        restore_after_s: float = 20.0,
        min_owner_hold_s: float = 10.0,
        max_owner_hold_s: float = 35.0,
        min_switch_interval_s: float = 5.0,
        yellow_transition_s: float = 3.0,
        all_red_s: float = 0.0,
        min_dynamic_green_s: float = 8.0,
        max_dynamic_green_s: float = 55.0,
        queue_gain_s: float = 2.0,
        post_restore_cooldown_s: float = 8.0,
    ) -> None:
        self.preemption_phases = preemption_phases
        self.lookahead_m = float(lookahead_m)
        self.hold_green_s = float(hold_green_s)
        self.restore_after_s = float(restore_after_s)
        self.min_owner_hold_s = max(0.0, float(min_owner_hold_s))
        self.max_owner_hold_s = float(max_owner_hold_s)
        self.min_switch_interval_s = float(min_switch_interval_s)
        self.yellow_transition_s = max(0.0, float(yellow_transition_s))
        self.all_red_s = max(0.0, float(all_red_s))
        self.min_dynamic_green_s = max(8.0, float(min_dynamic_green_s))
        self.max_dynamic_green_s = max(self.min_dynamic_green_s, float(max_dynamic_green_s))
        self.queue_gain_s = max(0.0, float(queue_gain_s))
        self.post_restore_cooldown_s = max(0.0, float(post_restore_cooldown_s))

        self._baseline: dict[str, tuple[str, int, float]] = {}
        self._active_ts: dict[str, float] = {}
        self._tls_owner: dict[str, str] = {}
        self._tls_owner_since: dict[str, float] = {}
        self._recent_restore_until: dict[str, float] = {}
        self._last_switch_ts: dict[str, float] = {}
        self._pending_stage_until: dict[str, float] = {}
        self._pending_stage_name: dict[str, str] = {}
        self._pending_link_idx: dict[str, int] = {}
        self._last_refresh_ts: dict[str, float] = {}
        self._refresh_interval_s: float = 4.0

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

    def _set_transition_stage(self, traci, tls_id: str, sim_time: float, owner: str, link_index: int) -> None:
        try:
            logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
        except Exception:
            return

        try:
            lane_count = len(logic.phases[0].state)
        except Exception:
            lane_count = 0
        if lane_count <= 0:
            return

        duration = 0.0
        state = ""
        stage_name = ""
        if self.yellow_transition_s > 0.0:
            state = "y" * lane_count
            duration = self.yellow_transition_s
            stage_name = "yellow"
        elif self.all_red_s > 0.0:
            state = "r" * lane_count
            duration = self.all_red_s
            stage_name = "all_red"
        else:
            return

        all_red_state = "r" * lane_count
        try:
            traci.trafficlight.setRedYellowGreenState(tls_id, state if stage_name == "yellow" else all_red_state)
            traci.trafficlight.setPhaseDuration(tls_id, duration)
            self._pending_stage_until[tls_id] = float(sim_time) + duration
            self._pending_stage_name[tls_id] = stage_name
            self._pending_link_idx[tls_id] = int(link_index)
            print(
                f"[SIGNAL] t={sim_time:.1f}s tls={tls_id} owner={owner} "
                f"{stage_name}-start duration={duration:.1f}s"
            )
        except Exception:
            return

    def _demand_adaptive_green_hold(self, traci, tls_id: str) -> float:
        demand_score = 0.0
        try:
            controlled_lanes = traci.trafficlight.getControlledLanes(tls_id)
        except Exception:
            controlled_lanes = []

        seen = set()
        for lane_id in controlled_lanes:
            if lane_id in seen:
                continue
            seen.add(lane_id)
            try:
                halted = float(traci.lane.getLastStepHaltingNumber(lane_id))
            except Exception:
                halted = 0.0
            demand_score += halted

        hold = max(self.hold_green_s, self.min_dynamic_green_s) + demand_score * self.queue_gain_s
        return min(self.max_dynamic_green_s, max(self.min_dynamic_green_s, hold))

    def _release_pending_transition(self, traci, sim_time: float) -> None:
        if not self._pending_stage_until:
            return

        ready_ids = [
            tls_id
            for tls_id, due_ts in self._pending_stage_until.items()
            if sim_time >= due_ts
        ]
        for tls_id in ready_ids:
            stage_name = self._pending_stage_name.get(tls_id, "")
            link_idx = int(self._pending_link_idx.get(tls_id, 0))

            # Yellow -> all-red transition before forcing emergency green.
            if stage_name == "yellow" and self.all_red_s > 0.0:
                try:
                    logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
                    lane_count = len(logic.phases[0].state)
                except Exception:
                    lane_count = 0

                if lane_count > 0:
                    try:
                        traci.trafficlight.setRedYellowGreenState(tls_id, "r" * lane_count)
                        traci.trafficlight.setPhaseDuration(tls_id, self.all_red_s)
                        self._pending_stage_until[tls_id] = float(sim_time) + self.all_red_s
                        self._pending_stage_name[tls_id] = "all_red"
                        owner = self._tls_owner.get(tls_id, "unknown")
                        print(
                            f"[SIGNAL] t={sim_time:.1f}s tls={tls_id} owner={owner} "
                            f"all_red-start duration={self.all_red_s:.1f}s"
                        )
                        continue
                    except Exception:
                        pass

            desired_phase = self.preemption_phases.get(tls_id)
            if desired_phase is None:
                desired_phase = self._find_phase_for_link(traci, tls_id, link_idx)
            if desired_phase is None:
                self._pending_stage_until.pop(tls_id, None)
                self._pending_stage_name.pop(tls_id, None)
                self._pending_link_idx.pop(tls_id, None)
                continue

            try:
                traci.trafficlight.setPhase(tls_id, int(desired_phase))
                dynamic_hold = self._demand_adaptive_green_hold(traci, tls_id)
                traci.trafficlight.setPhaseDuration(tls_id, dynamic_hold)
                self._active_ts[tls_id] = float(sim_time)
                self._last_switch_ts[tls_id] = float(sim_time)
                self._last_refresh_ts[tls_id] = float(sim_time)
                owner = self._tls_owner.get(tls_id, "unknown")
                print(
                    f"[SIGNAL] t={sim_time:.1f}s tls={tls_id} owner={owner} "
                    f"phase-> {desired_phase} dynamic-green={dynamic_hold:.1f}s"
                )
            except Exception:
                pass

            self._pending_stage_until.pop(tls_id, None)
            self._pending_stage_name.pop(tls_id, None)
            self._pending_link_idx.pop(tls_id, None)

    def _activate(self, traci, tls_id: str, link_index: int, sim_time: float) -> None:
        self._store_baseline(traci, tls_id)

        # Keep an already preempted TLS stable; refresh hold timer occasionally only.
        if tls_id in self._active_ts and tls_id not in self._pending_stage_until:
            last_refresh = float(self._last_refresh_ts.get(tls_id, -1e9))
            if (float(sim_time) - last_refresh) >= self._refresh_interval_s:
                dynamic_hold = self._demand_adaptive_green_hold(traci, tls_id)
                try:
                    traci.trafficlight.setPhaseDuration(tls_id, dynamic_hold)
                except Exception:
                    pass
                self._last_refresh_ts[tls_id] = float(sim_time)
            self._active_ts[tls_id] = float(sim_time)
            return

        if tls_id in self._pending_stage_until:
            return

        if self.yellow_transition_s > 0.0 or self.all_red_s > 0.0:
            owner = self._tls_owner.get(tls_id, "unknown")
            self._set_transition_stage(traci, tls_id, sim_time, owner, link_index)
            return

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
        dynamic_hold = self._demand_adaptive_green_hold(traci, tls_id)
        traci.trafficlight.setPhaseDuration(tls_id, dynamic_hold)
        self._active_ts[tls_id] = sim_time
        self._last_refresh_ts[tls_id] = float(sim_time)

    def preempt_for_vehicle(self, traci, vehicle_id: str) -> None:
        self.preempt_for_vehicles(traci, [vehicle_id], {vehicle_id: 1.0})

    def _can_switch_owner(self, tls_id: str, sim_time: float, new_owner: str) -> bool:
        restored_until = float(self._recent_restore_until.get(tls_id, -1e9))
        if sim_time < restored_until:
            return False

        current_owner = self._tls_owner.get(tls_id)
        if not current_owner or current_owner == new_owner:
            return True

        owner_since = self._tls_owner_since.get(tls_id, sim_time)
        last_switch = self._last_switch_ts.get(tls_id, -1e9)

        # Avoid rapid oscillation and provide fairness before ownership changes.
        if sim_time - last_switch < self.min_switch_interval_s:
            return False
        if sim_time - owner_since < self.min_owner_hold_s:
            return False
        if sim_time - owner_since < self.max_owner_hold_s:
            return False

        return True

    def preempt_for_vehicles(self, traci, vehicle_ids: list[str], priority_by_vehicle: dict[str, float]) -> None:
        sim_time = traci.simulation.getTime()
        self._release_pending_transition(traci, float(sim_time))
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
            self._pending_stage_until.pop(tls_id, None)
            self._pending_stage_name.pop(tls_id, None)
            self._pending_link_idx.pop(tls_id, None)
            self._last_refresh_ts.pop(tls_id, None)
            self._recent_restore_until[tls_id] = float(sim_time) + self.post_restore_cooldown_s

    def active_tls_count(self) -> int:
        return len(self._active_ts)

    def active_tls_ids(self) -> list[str]:
        return list(self._active_ts.keys())

    def tls_owner_map(self) -> dict[str, str]:
        return dict(self._tls_owner)
