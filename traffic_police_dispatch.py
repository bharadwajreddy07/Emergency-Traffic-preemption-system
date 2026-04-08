import json
import os
import time
import base64
from urllib import parse, request


def _send_twilio_sms(to_number: str, message: str) -> tuple[bool, str]:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if not account_sid or not auth_token or not from_number or not to_number:
        return False, "twilio_env_missing"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    body = parse.urlencode({"To": to_number, "From": from_number, "Body": message}).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    token = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", "Basic " + token)
    try:
        with request.urlopen(req, timeout=4):
            return True, ""
    except Exception as exc:
        return False, str(exc)


def send_police_notification(
    endpoint: str,
    sms_to: str,
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

    sms_status = "not_configured"
    sms_error = ""
    if sms_to.strip():
        sms_message = (
            f"Emergency {vehicle_id} type={emergency_type} eta={payload['eta_seconds']}s "
            f"corridor_tls={len(corridor_tls_ids)} reason={reroute_reason}"
        )
        ok, err = _send_twilio_sms(sms_to.strip(), sms_message)
        sms_status = "sent" if ok else "failed"
        sms_error = err
    payload["police_sms_to"] = sms_to.strip()
    payload["police_sms_status"] = sms_status

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
                return {"status": "sent", "channel": "http", "sms_status": sms_status, "payload": payload}
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
        "sms_status": sms_status,
        "error": error_text,
        "sms_error": sms_error,
        "payload": payload,
    }
