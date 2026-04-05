# Intelligent Emergency Vehicle Management (Hyderabad + SUMO)

This project implements an edge-first emergency corridor system for SUMO that supports:

1. Dual verification trigger:
	- Siren confidence score from local ML/microphone input
	- Wireless trigger input (LoRa/RF/GPS gateway)
2. Automatic multi-intersection green corridor (TraCI preemption)
3. Dynamic rerouting to nearest suitable hospital using live SUMO travel time and shortest-path route search
4. Hospital pre-notification with ETA, vehicle ID, emergency type
5. Automatic restoration of normal traffic light programs after ambulance clearance

The design does not require internet for junction-level control.

## Project files

- `smart_emergency_system.py`: end-to-end orchestrator
- `detection_fusion.py`: false-trigger-resistant dual verification logic
- `signal_preemption.py`: green corridor + traffic light restoration
- `route_planner.py`: ETA estimation and route application
- `hospital_dispatch.py`: hospital selection and pre-notification
- `hyderabad_hospitals.csv`: starter hospital registry
- `config/hyderabad_example.json`: preemption and network mapping template
- `generate_hospital_markers.py`: creates map-style real-world hospital symbols for SUMO GUI
- `ensure_traffic_signals.py`: validates TLS presence and rebuilds net from OSM with guessed signals when needed

## 1) Prerequisites

Install:

- SUMO (latest stable)
- Python 3.10+

Set environment variable:

- Windows PowerShell:

```powershell
$env:SUMO_HOME = "C:\Program Files (x86)\Eclipse\Sumo"
```

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

## 2) Build Hyderabad SUMO map

Option A: Download Hyderabad OSM extract and convert with `netconvert`.

```powershell
netconvert --osm-files hyderabad.osm.xml --output-file hyderabad.net.xml
```

Create trips/routes (example):

```powershell
python "%SUMO_HOME%\tools\randomTrips.py" -n hyderabad.net.xml -o hyderabad.trips.xml -r hyderabad.rou.xml -e 3600
```

Create a SUMO config file (for example `hyderabad.sumocfg`) that references:

- `hyderabad.net.xml`
- `hyderabad.rou.xml`

You can generate `hyderabad.sumocfg` automatically:

```powershell
python create_hyderabad_sumocfg.py --net-file hyderabad.net.xml --route-files hyderabad.rou.xml,emergency_vehicle.rou.xml --out hyderabad.sumocfg
```

## 2.1) Use all Hyderabad hospitals automatically (not only 3-4)

If you have `hyderabad.osm.xml` and `hyderabad.net.xml`, run:

```powershell
python generate_hyderabad_hospitals_from_osm.py `
  --osm hyderabad.osm.xml `
  --net hyderabad.net.xml `
  --out-csv hyderabad_hospitals.csv `
  --out-edge-map config/hospital_edge_map.generated.json `
  --update-config config/hyderabad_example.json
```

This command does all of the following:

1. Extracts all OSM hospitals in Hyderabad.
2. Maps each hospital to the nearest drivable SUMO edge.
3. Regenerates `hyderabad_hospitals.csv` with the full list.
4. Updates `hospital_edge_map` in `config/hyderabad_example.json` so rerouting works city-wide.

After this step, the controller reroutes across the full generated hospital list.

If your downloaded map file is named `map.osm`, use this command directly:

```powershell
python generate_hyderabad_hospitals_from_osm.py --osm map.osm --net hyderabad.net.xml --out-csv hyderabad_hospitals.csv --out-edge-map config/hospital_edge_map.generated.json --update-config config/hyderabad_example.json
```

## 3) Add emergency vehicle and TLS phase mapping

1. Ensure an emergency vehicle exists in your route file (for example `ambulance_1` with type `emergency`).
2. You can create one or more ambulances automatically from existing valid car routes:

```powershell
python create_multi_ambulance_routes.py --base-route hyderabad_car.rou.xml --out emergency_vehicle.rou.xml --count 12 --depart-start 10 --depart-gap 18
```

`--count` is enforced to 10..15. Each ambulance is generated with blue-light/siren parameters.

3. `config/hyderabad_example.json` supports two modes:
  - Auto mode (recommended): keep `preemption_phases` empty and the controller finds a green phase from link index.
  - Manual mode: set explicit TLS phase indices if you want strict control at selected junctions.

Useful TLS discovery during test:

```powershell
python - << 'PY'
import os, sys
sys.path.append(os.path.join(os.environ['SUMO_HOME'],'tools'))
import sumolib
net=sumolib.net.readNet('hyderabad.net.xml')
print('TLS count:', len(net.getTrafficLights()))
for tls in net.getTrafficLights()[:10]:
	 print(tls.getID())
PY
```

## 4) Sensor input integration (edge/local)

The orchestrator reads two local files continuously:

- `out/mic_score.txt` -> float in [0,1]
- `out/wireless_signal.txt` -> one of: `1`, `true`, `yes`, `on`, `detected`

This allows you to connect any local ML/LoRa process without cloud dependency.

## 5) Run the integrated system

```powershell
python smart_emergency_system.py `
  --sumocfg hyderabad.sumocfg `
  --sumo-binary sumo-gui `
  --vehicle-id ambulance_1 `
  --emergency-type trauma `
  --mic-score-file out/mic_score.txt `
  --wireless-file out/wireless_signal.txt `
  --hospitals-csv hyderabad_hospitals.csv `
  --config config/hyderabad_example.json
```

For your current workspace, this single command runs the full setup and launch:

```powershell
.\run_hyderabad.ps1
```

To reduce regular congestion from cars and bikes during emergency validation:

```powershell
.\run_hyderabad.ps1 -CarTrafficScale 0.6 -BikeTrafficScale 0.5
```

Lower scale means fewer generated trips (for example, `0.5` means about half baseline demand).

Strict production mode (high traffic + multiple ambulances + conflict handling + health-check gate):

```powershell
.\run_hyderabad.ps1 -Profile strict-production -SumoBinary sumo-gui
```

Headless production run:

```powershell
.\run_hyderabad.ps1 -Profile strict-production -SumoBinary sumo
```

The runner now does all of this before launch:

1. Generates high-density mixed traffic demand (cars + bikes + pedestrians).
2. Creates multiple ambulance vehicles.
3. Builds sumocfg with car + bike + pedestrian + emergency route files.
4. Regenerates hospital-edge map from OSM.
5. Generates hospital markers with red plus symbol overlays.
6. Runs `health_check.py` and blocks launch if reachability is below threshold.
7. Runs `ensure_traffic_signals.py` and auto-rebuilds the network from `map.osm` if no traffic signals exist.
8. Runs with strict-production ambulance volume (10-15) for congestion stress testing.
9. Generates dedicated shortest hospital paths in `config/hospital_priority_paths.generated.json`.

This command links your SUMO map and Python logic as follows:

1. `--sumocfg hyderabad.sumocfg`: loads Hyderabad network and routes.
2. `--hospitals-csv hyderabad_hospitals.csv`: loads all hospitals.
3. `--config config/hyderabad_example.json`: loads hospital-to-edge mapping and signal preemption setup.
4. Controller computes live ETA to every mapped hospital and reroutes automatically.

Headless mode:

```powershell
python smart_emergency_system.py --sumocfg hyderabad.sumocfg --sumo-binary sumo
```

## 5.2) Phase-1 Real-World Data + Police Alerts

The controller now supports realtime roadside ingestion, LoRa map-matching, merged routing costs in `dijkstra/astar`, and police notifications.

### New runtime options

```powershell
python smart_emergency_system.py `
  --sumocfg hyderabad.sumocfg `
  --routing-algorithm dijkstra `
  --live-traffic-file out/live_traffic.json `
  --lora-events-file out/lora_events.jsonl `
  --police-log out/police_notifications.jsonl `
  --write-web-state
```

Optional API endpoint for traffic police command center:

```powershell
python smart_emergency_system.py --sumocfg hyderabad.sumocfg --police-endpoint http://127.0.0.1:9000/police/alerts
```

### `out/live_traffic.json` format

```json
{
  "timestamp": 1710000000,
  "edges": [
    {
      "edge_id": "25286049#0",
      "speed_kmh": 14.0,
      "occupancy": 0.72,
      "confidence": 0.9,
      "incident": false
    }
  ]
}
```

### `out/lora_events.jsonl` format

Each line is one JSON object:

```json
{"timestamp": 1710000000, "ambulance_id": "ambulance_1", "lat": 17.4401, "lon": 78.3902, "emergency": true}
```

### Dashboard fields added

Web dashboard now shows:

1. Trigger confidence
2. Selected corridor TLS IDs
3. Reroute reason (per ambulance)
4. Police notification status

### Mock realtime generator (no hardware required)

You can generate both `out/live_traffic.json` and `out/lora_events.jsonl` with a single helper script:

```powershell
python generate_mock_realtime_inputs.py --net-file hyderabad.net.xml --truncate-lora --interval-s 1.0
```

Run for a fixed number of ticks (for test automation):

```powershell
python generate_mock_realtime_inputs.py --iterations 30 --truncate-lora
```

Recommended full demo (three terminals):

1. Generate mock data continuously:

```powershell
python generate_mock_realtime_inputs.py --truncate-lora
```

2. Run emergency controller using merged realtime inputs:

```powershell
python smart_emergency_system.py `
  --sumocfg hyderabad.sumocfg `
  --sumo-binary sumo-gui `
  --routing-algorithm dijkstra `
  --live-traffic-file out/live_traffic.json `
  --lora-events-file out/lora_events.jsonl `
  --police-log out/police_notifications.jsonl `
  --write-web-state
```

3. Start dashboard server:

```powershell
python web/realtime_server.py --host 127.0.0.1 --port 8090
```

## 5.1) Parallel Realtime Web Dashboard

SUMO workflow remains unchanged; you can run a parallel web map using exported realtime state.

1. Start simulation as usual:

```powershell
.\run_hyderabad.ps1
```

2. In a second terminal, launch dashboard server:

```powershell
python web/realtime_server.py --host 127.0.0.1 --port 8090
```

3. Open:

```text
http://127.0.0.1:8090
```

## 6) Hyderabad hospital customization

Edit `hyderabad_hospitals.csv` with:

- Current capacity values
- Specialization flags (`supports_trauma`, `supports_cardiac`, `supports_stroke`)
- Local HTTP endpoint for each hospital if available

If endpoint is blank/offline, notifications are stored in:

- `out/hospital_notifications.jsonl`

## 7) How rerouting works

For each verification event, the system:

1. Calculates ETA from ambulance current edge to each hospital entry edge using SUMO route finding.
2. Filters hospitals by emergency type and available capacity.
3. Chooses best score (ETA with capacity preference).
4. Applies route update via TraCI.
5. Pre-notifies destination hospital.

Implementation location for rerouting:

1. `route_planner.py` contains ETA estimation and route construction.
2. `smart_emergency_system.py` invokes rerouting every configured interval (`--reroute-interval-s` / profile override).
3. You can select shortest-path engine: `--routing-algorithm sumo|dijkstra|astar`.
4. For `dijkstra` and `astar`, route cost uses live SUMO edge travel times (congestion-aware) with static fallback.

Implementation location for dedicated emergency priority:

1. `emergency_vehicle.rou.xml` and `create_multi_ambulance_routes.py` define `vClass="emergency"` and blue-light/siren parameters.
2. `signal_preemption.py` enforces multi-intersection green-corridor preemption.
3. `smart_emergency_system.py` applies runtime emergency parameters and preemption ownership fairness.

Current limitation:

1. Physical dedicated ambulance-only lanes depend on lane permissions in `hyderabad.net.xml`.
2. This project currently prioritizes ambulances through dynamic rerouting + signal preemption, not static lane redesign.

## 7.1) Multi-ambulance priority conflict handling

When two ambulances request conflicting movements at one traffic signal:

1. Controller scores incoming requests by priority and approach distance.
2. It grants ownership of the signal to one ambulance for a bounded hold window.
3. It prevents rapid oscillation via minimum switch interval.
4. It rebalances and hands over to the next ambulance after fairness window expires.

## 7.2) GUI visualization

When you run with `-SumoBinary sumo-gui`, the simulation shows:

1. Hospital markers as red plus symbols from `hospital_markers.add.xml`.
2. Emergency vehicles in bright dedicated colors.
3. Signal preemption logs in terminal (`[SIGNAL] ...`) showing phase changes and restoration.
4. Mixed traffic with cars, bikes, and pedestrians.

## 8) Green Corridor Tracking (Detailed)

This project tracks and enforces green corridor behavior through these runtime stages:

1. Emergency vehicle detection:
  `smart_emergency_system.py` identifies active ambulances by type/id.
2. Junction scan:
  For each ambulance, `traci.vehicle.getNextTLS` is used to find upcoming signals.
3. Priority scoring:
  Closer ambulances with higher emergency priority get stronger score.
4. Signal control:
  `signal_preemption.py` sets the desired phase to green for the winning ambulance movement.
5. Ownership fairness:
  Ownership windows avoid rapid oscillation between ambulances.
6. Restore logic:
  Signals are restored to baseline after corridor window ends.

Telemetry keys:

1. `[SIGNAL] ...` lines show preemption and restore actions.
2. `[ASSERT] ... active_tls_preempted=` gives active preempted signal count.
3. Web dashboard `signals` layer shows red/yellow-or-orange/green with preempted flag.

## 9) Realtime Dashboard (Project Ops)

Preferred run command (single command):

```powershell
.\run_hyderabad.ps1 -StartWebDashboard
```

If started manually, use workspace-relative path:

```powershell
python web/realtime_server.py --host 127.0.0.1 --port 8090
```

Open browser:

```text
http://127.0.0.1:8090
```

Dashboard capabilities:

1. Ambulance status: `enroute | reached | breakdown`.
2. Arrival timeline with elapsed dispatch-to-hospital times.
3. Signal markers with current color and preemption status.
4. Layer switcher including satellite basemap for flyover inspection.
5. Popup notifications for reached/breakdown transitions.

## 10) Route and Hospital Assignment Policy

To avoid fleet pile-up at one hospital:

1. Hospital ETA is adjusted with load penalty (`hospital_load_by_id`).
2. Assigned hospital is kept stable per ambulance to avoid oscillation.
3. Stop positions are lane/position staggered per hospital (`hospital_stop_slots`).
4. Hospital load is released when ambulance reaches or breaks down.

## 11) Event Semantics

1. `ARRIVAL`:
  Logged once when ambulance is on destination hospital edge and nearly stopped.
2. `BREAKDOWN`:
  Logged when vehicle remains stopped beyond configured threshold on non-internal edge.
3. `SUMMARY`:
  Logged when all ambulances are completed (reached + breakdown).

## 12) Recommended Validation Checklist

1. Confirm at least one signal preemption in `[SIGNAL]` logs.
2. Confirm dashboard shows non-zero `active_tls_preempted` during emergency windows.
3. Confirm arrivals populate timeline with elapsed seconds.
4. Confirm no repeated arrival line for same ambulance.
5. Confirm hospitals are load-distributed across fleet under congestion.
5. Ambulance debug camera mode: auto-tracks and highlights active ambulances in SUMO GUI.

You can tune camera behavior with controller options:

1. `--ambulance-debug-gui`
2. `--camera-switch-interval-s 5.0`
3. `--camera-zoom 1700`

## 7.3) LoRa integration (UDP gateway)

If your LoRa gateway forwards packets over UDP, run:

```powershell
python smart_emergency_system.py --sumocfg hyderabad.sumocfg --sumo-binary sumo-gui --auto-detect-emergency-vehicles --lora-udp-host 127.0.0.1 --lora-udp-port 1700
```

Any UDP payload received on that socket is treated as a wireless trigger.

Runner passthrough parameters:

```powershell
.\run_hyderabad.ps1 -Profile strict-production -SumoBinary sumo-gui -LoraUdpHost 127.0.0.1 -LoraUdpPort 1700
```

## 10) Project objectives presentation

Generate an objectives deck:

```powershell
python create_project_objectives_ppt.py
```

Output file:

- `Project_Objectives_Emergency_Traffic.pptx`

## 8) Scaling to city grid

For wider smart-city scaling:

1. Partition the city into control zones.
2. Run one edge controller per zone.
3. Use corridor handoff by forwarding vehicle ID + route corridor to adjacent zone controller.
4. Keep local fallback logic if inter-zone communication drops.

## 9) Quick validation checklist

1. Dual verification must be true before preemption occurs.
2. Corridor preemption should affect multiple upcoming intersections.
3. Route should change when traffic/ETA changes.
4. Hospital notification should appear in endpoint or local JSONL log.
5. Traffic lights should return to baseline after ambulance passes.

