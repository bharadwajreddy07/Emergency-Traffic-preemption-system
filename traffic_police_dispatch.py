import json
import os
import time
from urllib import request


def send_police_notification(
    endpoint: str,
    fallback_log_path: str,
    vehicle_id: str,
    emergency_type: str,
    eta_seconds: float | None,
    corridor_tls_ids: list[str],
    trigger_confidence: float,
    reroute_reason: str,
) -> dict:
    payload = {
        "timestamp": int(time.time()),
        "vehicle_id": vehicle_id,
        "emergency_type": emergency_type,
        "eta_seconds": None if eta_seconds is None else round(float(eta_seconds), 1),
        "selected_corridor_tls": list(corridor_tls_ids),
        "trigger_confidence": round(float(trigger_confidence), 3),
        "reroute_reason": reroute_reason,
    }

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=2):
                return {"status": "sent", "channel": "http", "payload": payload}
        except Exception as exc:
            error_text = str(exc)
        else:
            error_text = "unknown"
    else:
        error_text = ""

    os.makedirs(os.path.dirname(fallback_log_path) or ".", exist_ok=True)
    with open(fallback_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")

    return {
        "status": "logged",
        "channel": "file",
        "error": error_text,
        "payload": payload,
    }
