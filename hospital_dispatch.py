import csv
import math
import json
import os
import time
from dataclasses import dataclass
from typing import Iterable
from urllib import request


SPECIALTY_FIELDS = {
    "trauma": "supports_trauma",
    "cardiac": "supports_cardiac",
    "stroke": "supports_stroke",
    "general": None,
}


@dataclass
class Hospital:
    hospital_id: str
    name: str
    lat: float
    lon: float
    capacity_available: int
    supports_trauma: bool
    supports_cardiac: bool
    supports_stroke: bool
    endpoint: str


class HospitalRegistry:
    def __init__(self, csv_path: str) -> None:
        self.csv_path = csv_path
        self.hospitals = self._load(csv_path)

    @staticmethod
    def _to_bool(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "y"}

    def _load(self, csv_path: str) -> list[Hospital]:
        items: list[Hospital] = []
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                items.append(
                    Hospital(
                        hospital_id=row["hospital_id"],
                        name=row["name"],
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                        capacity_available=int(row["capacity_available"]),
                        supports_trauma=self._to_bool(row.get("supports_trauma", "0")),
                        supports_cardiac=self._to_bool(row.get("supports_cardiac", "0")),
                        supports_stroke=self._to_bool(row.get("supports_stroke", "0")),
                        endpoint=row.get("endpoint", "").strip(),
                    )
                )
        return items

    def suitable_hospitals(self, emergency_type: str) -> list[Hospital]:
        field_name = SPECIALTY_FIELDS.get(emergency_type.lower(), None)
        result = []
        for hospital in self.hospitals:
            if hospital.capacity_available <= 0:
                continue
            if field_name is not None and not getattr(hospital, field_name):
                continue
            result.append(hospital)
        return result

    def select_best(self, emergency_type: str, eta_seconds: dict[str, float]) -> Hospital | None:
        candidates = self.suitable_hospitals(emergency_type)
        if not candidates:
            return None

        reachable = [h for h in candidates if math.isfinite(eta_seconds.get(h.hospital_id, float("inf")))]
        if not reachable:
            return None

        def score(h: Hospital) -> float:
            eta = eta_seconds.get(h.hospital_id, float("inf"))
            capacity_bonus = min(20.0, h.capacity_available * 0.4)
            return eta - capacity_bonus

        return min(reachable, key=score)


def send_hospital_pre_notification(
    hospital: Hospital,
    vehicle_id: str,
    emergency_type: str,
    eta_seconds: float,
    fallback_log_path: str,
) -> None:
    payload = {
        "timestamp": int(time.time()),
        "hospital_id": hospital.hospital_id,
        "vehicle_id": vehicle_id,
        "emergency_type": emergency_type,
        "eta_seconds": round(float(eta_seconds), 1),
    }

    # If endpoint is an HTTP service, send a local POST call.
    if hospital.endpoint.startswith("http://") or hospital.endpoint.startswith("https://"):
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            hospital.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=2):
                return
        except Exception:
            pass

    # Fallback local log for offline and edge-only operation.
    os.makedirs(os.path.dirname(fallback_log_path) or ".", exist_ok=True)
    with open(fallback_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")
