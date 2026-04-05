import os
import time
from dataclasses import dataclass


@dataclass
class DetectionConfig:
    siren_threshold: float = 0.75
    min_siren_hits: int = 3
    wireless_grace_seconds: float = 4.0
    confirmation_cooldown_seconds: float = 8.0


class DualVerificationDetector:
    """Fuse siren ML score and wireless signal to reduce false triggering."""

    def __init__(self, config: DetectionConfig | None = None) -> None:
        self.config = config or DetectionConfig()
        self._siren_hits = 0
        self._last_wireless_ts = -1.0
        self._last_confirmation_ts = -1.0

    def update(self, siren_score: float, wireless_seen: bool, now_ts: float | None = None) -> bool:
        now_ts = now_ts if now_ts is not None else time.time()

        if siren_score >= self.config.siren_threshold:
            self._siren_hits += 1
        else:
            self._siren_hits = max(0, self._siren_hits - 1)

        if wireless_seen:
            self._last_wireless_ts = now_ts

        wireless_recent = (
            self._last_wireless_ts > 0
            and now_ts - self._last_wireless_ts <= self.config.wireless_grace_seconds
        )
        siren_consistent = self._siren_hits >= self.config.min_siren_hits

        in_cooldown = (
            self._last_confirmation_ts > 0
            and now_ts - self._last_confirmation_ts <= self.config.confirmation_cooldown_seconds
        )

        confirmed = bool(wireless_recent and siren_consistent and not in_cooldown)
        if confirmed:
            self._last_confirmation_ts = now_ts
            self._siren_hits = 0

        return confirmed


def read_float_file(path: str, default: float = 0.0) -> float:
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return float(f.read().strip())
    except (ValueError, OSError):
        return default


def read_wireless_file(path: str) -> bool:
    """Interpret wireless input file as one of: 1/0, true/false, yes/no."""
    if not path or not os.path.exists(path):
        return False

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip().lower()
    except OSError:
        return False

    return text in {"1", "true", "yes", "on", "detected"}
