param(
    [Alias('Profile')]
    [ValidateSet('strict-production', 'standard')]
    [string]$Mode = 'strict-production',
    [ValidateSet('sumo-gui', 'sumo')]
    [string]$SumoBinary = 'sumo-gui',
    [int]$AmbulanceCount = 20,
    [switch]$HideNonEmergencyLabels,
    [switch]$StartWebDashboard,
    [int]$WebDashboardPort = 8090,
    [ValidateRange(0.1, 1.0)]
    [double]$CarTrafficScale = 0.7,
    [ValidateRange(0.1, 1.0)]
    [double]$BikeTrafficScale = 0.7,
    [int]$StepLimit = 0,
    [string]$LoraUdpHost = '127.0.0.1',
    [int]$LoraUdpPort = 0,
    [string]$PoliceMobile = ''
)

$ErrorActionPreference = 'Stop'

if (-not $env:SUMO_HOME) {
    throw 'SUMO_HOME is not set. Example: $env:SUMO_HOME = "C:\Program Files (x86)\Eclipse\Sumo"'
}

$py = 'C:/Users/Dell/AppData/Local/Programs/Python/Python313/python.exe'
$modeConfigPath = if ($Mode -eq 'strict-production') { 'config/profiles/strict_production.json' } else { '' }

if (-not (Test-Path 'hyderabad.net.xml')) {
    throw 'hyderabad.net.xml not found in workspace root.'
}
if (-not (Test-Path 'map.osm')) {
    throw 'map.osm not found in workspace root.'
}
if (-not (Test-Path $py)) {
    throw "Python executable not found at $py"
}

$duration = 7200
$period = 1.2
$bikePeriod = 3.0
$pedPeriod = 4.0
$ambulanceCount = $AmbulanceCount
$departStart = 10
$departGap = 30
$minReachability = 0.8

if ($Mode -eq 'strict-production') {
    $modeConfig = Get-Content -Raw -Path $modeConfigPath | ConvertFrom-Json
    $duration = [int]$modeConfig.traffic.duration_s
    $period = [double]$modeConfig.traffic.trip_period_s
    $bikePeriod = [double]$modeConfig.traffic.bike_trip_period_s
    $pedPeriod = [double]$modeConfig.traffic.ped_trip_period_s
    if (-not $PSBoundParameters.ContainsKey('AmbulanceCount')) {
        $ambulanceCount = [int]$modeConfig.ambulance.count
    }
    $departStart = [double]$modeConfig.ambulance.depart_start_s
    $departGap = [double]$modeConfig.ambulance.depart_gap_s
    $minReachability = [double]$modeConfig.healthcheck.min_reachability_ratio
}

if ($ambulanceCount -lt 1) {
    throw "Ambulance count must be >= 1. Current value: $ambulanceCount"
}
$effectiveCarPeriod = [Math]::Round(($period / $CarTrafficScale), 3)
$effectiveBikePeriod = [Math]::Round(($bikePeriod / $BikeTrafficScale), 3)

Write-Host "[RUN] Ensuring traffic signals exist in the network"
& $py ensure_traffic_signals.py --net hyderabad.net.xml --osm map.osm --require-min 1

Write-Host "[RUN] Generating car traffic demand (duration=$duration, period=$effectiveCarPeriod, scale=$CarTrafficScale)"
& $py "$env:SUMO_HOME/tools/randomTrips.py" -n hyderabad.net.xml -o hyderabad.trips.xml -r hyderabad_car.rou.xml -e $duration -p $effectiveCarPeriod --seed 42 --validate

Write-Host "[RUN] Generating bike traffic demand (period=$effectiveBikePeriod, scale=$BikeTrafficScale)"
& $py "$env:SUMO_HOME/tools/randomTrips.py" -n hyderabad.net.xml -o hyderabad_bike.trips.xml -r hyderabad_bike.rou.xml -e $duration -p $effectiveBikePeriod --seed 43 --vehicle-class bicycle --vclass bicycle --prefix bike --validate

Write-Host "[RUN] Generating pedestrian demand (period=$pedPeriod)"
& $py "$env:SUMO_HOME/tools/randomTrips.py" -n hyderabad.net.xml -o hyderabad_ped.trips.xml -r hyderabad_ped.rou.xml -e $duration -p $pedPeriod --seed 44 --pedestrians --prefix ped --validate

Write-Host "[RUN] Creating stationed ambulance routes (3 per hospital)"
& $py create_stationed_ambulance_routes.py --config config/hyderabad_example.json --net hyderabad.net.xml --out emergency_vehicle.rou.xml --per-hospital 3
$ambulanceCount = (Select-String -Path emergency_vehicle.rou.xml -Pattern '<vehicle ').Count
Write-Host "[RUN] Stationed ambulances generated: $ambulanceCount"

Write-Host "[RUN] Building SUMO configuration"
& $py create_hyderabad_sumocfg.py --net-file hyderabad.net.xml --route-files hyderabad_car.rou.xml,hyderabad_bike.rou.xml,hyderabad_ped.rou.xml,emergency_vehicle.rou.xml --additional-files hospital_markers.add.xml --out hyderabad.sumocfg

Write-Host "[RUN] Generating hospital mapping from OSM"
& $py generate_hyderabad_hospitals_from_osm.py --osm map.osm --net hyderabad.net.xml --out-csv hyderabad_hospitals.csv --out-edge-map config/hospital_edge_map.generated.json --update-config config/hyderabad_example.json

Write-Host "[RUN] Generating dedicated hospital priority paths (Dijkstra)"
& $py generate_hospital_priority_paths.py --net hyderabad.net.xml --ambulance-routes emergency_vehicle.rou.xml --config config/hyderabad_example.json --out config/hospital_priority_paths.generated.json --algorithm dijkstra

Write-Host "[RUN] Generating hospital map-style markers"
if ($HideNonEmergencyLabels) {
    & $py generate_hospital_markers.py --net hyderabad.net.xml --hospitals-csv hyderabad_hospitals.csv --out hospital_markers.add.xml --symbol-size 9
} else {
    & $py generate_hospital_markers.py --net hyderabad.net.xml --hospitals-csv hyderabad_hospitals.csv --out hospital_markers.add.xml --with-labels --symbol-size 9
}

Write-Host "[RUN] Health check: hospital-edge reachability"
& $py health_check.py --sumocfg hyderabad.sumocfg --hospitals-csv hyderabad_hospitals.csv --config config/hyderabad_example.json --min-reachability-ratio $minReachability
if ($LASTEXITCODE -ne 0) {
    throw "Health check failed. Fix mapping/reachability before simulation run."
}

New-Item -ItemType Directory -Force out | Out-Null
Set-Content -Path out/mic_score.txt -Value '0.05'
Set-Content -Path out/wireless_signal.txt -Value 'off'
Set-Content -Path out/call_requests.jsonl -Value ''
Set-Content -Path out/trip_commands.jsonl -Value ''
Set-Content -Path out/control_commands.jsonl -Value ''

if ($StartWebDashboard) {
    Write-Host "[RUN] Starting realtime web dashboard on http://127.0.0.1:$WebDashboardPort"
    Start-Process -FilePath $py -ArgumentList @('web/realtime_server.py', '--host', '127.0.0.1', '--port', "$WebDashboardPort", '--call-file', 'out/call_requests.jsonl', '--trip-command-file', 'out/trip_commands.jsonl', '--control-command-file', 'out/control_commands.jsonl') -WorkingDirectory $PWD | Out-Null
}

Write-Host "[RUN] Launching emergency controller"
$controllerArgs = @(
    'smart_emergency_system.py',
    '--sumocfg', 'hyderabad.sumocfg',
    '--sumo-binary', $SumoBinary,
    '--auto-detect-emergency-vehicles',
    '--emergency-type', 'trauma',
    '--mic-score-file', 'out/mic_score.txt',
    '--wireless-file', 'out/wireless_signal.txt',
    '--call-requests-file', 'out/call_requests.jsonl',
    '--trip-commands-file', 'out/trip_commands.jsonl',
    '--control-commands-file', 'out/control_commands.jsonl',
    '--hospitals-csv', 'hyderabad_hospitals.csv',
    '--config', 'config/hyderabad_example.json',
    '--profile', $modeConfigPath,
    '--step-limit', $StepLimit,
    '--lora-udp-host', $LoraUdpHost,
    '--lora-udp-port', $LoraUdpPort,
    '--routing-algorithm', 'dijkstra',
    '--routing-net-file', 'hyderabad.net.xml',
    '--reroute-interval-s', '1.8',
    '--assert-interval-s', '10.0',
    '--hospital-stop-duration-s', '45.0',
    '--expected-ambulance-count', $ambulanceCount,
    '--write-web-state',
    '--web-state-file', 'out/realtime_state.json',
    '--web-state-interval-s', '1.0'
)

if (-not [string]::IsNullOrWhiteSpace($PoliceMobile)) {
    $controllerArgs += @('--police-sms-to', $PoliceMobile)
}

if ($SumoBinary -eq 'sumo-gui') {
    $controllerArgs += @(
        '--ambulance-debug-gui', '--camera-follow-mode', 'fleet', '--camera-switch-interval-s', '4.0', '--camera-zoom', '1700',
        '--status-panel-log', '--status-panel-interval-s', '1.0'
    )
    if ($HideNonEmergencyLabels) {
        $controllerArgs += @('--hide-non-emergency-labels')
    }
}

# Keep lane-clearance optional; forcing it can increase braking at dense conflict points.
# $controllerArgs += @('--force-lane-clearance', '--lane-clearance-lookahead-m', '55')

& $py @controllerArgs

Write-Host "[RUN] Optional realtime web dashboard: python web/realtime_server.py --host 127.0.0.1 --port $WebDashboardPort"
