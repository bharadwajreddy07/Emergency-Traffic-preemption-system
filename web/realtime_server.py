import argparse
import json
import os
import time
import hmac
import hashlib
import base64
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RealtimeHandler(BaseHTTPRequestHandler):
    state_file = "out/realtime_state.json"
    index_file = "web/index.html"
    call_file = "out/call_requests.jsonl"
    trip_command_file = "out/trip_commands.jsonl"
    control_command_file = "out/control_commands.jsonl"
    police_user = "traffic_police"
    police_password = "police123"
    app_user = "citizen_user"
    app_password = "user123"
    auth_secret = "emergency-command-secret"
    token_ttl_s = 12 * 60 * 60

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _append_call(path: str, payload: dict) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    @staticmethod
    def _append_trip_command(path: str, payload: dict) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    @staticmethod
    def _append_control_command(path: str, payload: dict) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    @classmethod
    def _issue_token(cls, username: str, role: str) -> str:
        exp = int(time.time()) + int(cls.token_ttl_s)
        role = role.strip() or "user"
        body = f"{username}:{role}:{exp}"
        sig = hmac.new(cls.auth_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
        raw = f"{body}:{sig}".encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @classmethod
    def _verify_token(cls, token: str) -> tuple[bool, str, str]:
        if not token:
            return False, "", ""
        try:
            decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
            parts = decoded.split(":")
            if len(parts) == 4:
                username, role, exp_str, sig = parts
            elif len(parts) == 3:
                # Backward compatibility for old tokens: username:exp:sig -> police role.
                username, exp_str, sig = parts
                role = "traffic_police"
            else:
                return False, "", ""
            exp = int(exp_str)
        except Exception:
            return False, "", ""

        if len(parts) == 4:
            body = f"{username}:{role}:{exp}"
        else:
            body = f"{username}:{exp}"
        expected = hmac.new(cls.auth_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False, "", ""
        if exp < int(time.time()):
            return False, "", ""
        return True, username, role

    def _parse_bearer(self) -> str:
        auth = str(self.headers.get("Authorization", "")).strip()
        if not auth.lower().startswith("bearer "):
            return ""
        return auth[7:].strip()

    def _read_state_payload(self) -> dict:
        if not os.path.exists(self.state_file):
            return {"timestamp": 0, "active_ambulances": 0, "ambulances": []}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"timestamp": 0, "active_ambulances": 0, "ambulances": []}

    @staticmethod
    def _build_police_overview(state: dict, username: str) -> dict:
        notifications = list(state.get("police_notifications", []))[-100:]
        calls = list(state.get("calls", []))[-100:]
        call_markers = list(state.get("call_markers", []))[-100:]
        ambulances = list(state.get("ambulances", []))
        active_dispatch = [
            a
            for a in ambulances
            if str(a.get("status", "")) == "enroute"
            and str(a.get("mission_phase", "")) in {"to_incident", "to_hospital"}
        ]
        return {
            "ok": True,
            "user": username,
            "timestamp": state.get("timestamp", 0),
            "active_ambulances": int(state.get("active_ambulances", 0) or 0),
            "active_tls_preempted": int(state.get("active_tls_preempted", 0) or 0),
            "notifications": notifications,
            "calls": calls,
            "call_markers": call_markers,
            "active_dispatch": active_dispatch,
            "trigger": state.get("trigger", {}),
            "live_feeds": state.get("live_feeds", {}),
            "selected_corridor_tls": state.get("selected_corridor_tls", []),
            "planned_corridor_tls": state.get("planned_corridor_tls", []),
            "planned_corridor_mode": state.get("planned_corridor_mode", "strict"),
            "corridor_route": state.get("corridor_route", []),
            "corridor_source": state.get("corridor_source", "none"),
        }

    def do_GET(self):
        target = str(self.path or "")
        parsed = urlsplit(target)
        path_only = parsed.path or target
        if "favicon.ico" in target.lower() or path_only.endswith("/favicon.ico") or path_only == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return

        if self.path in {"/", "/index.html"}:
            if not os.path.exists(self.index_file):
                self._send_html("<h1>index.html not found</h1>", status=404)
                return
            with open(self.index_file, "r", encoding="utf-8") as f:
                self._send_html(f.read())
            return

        if self.path.startswith("/api/state"):
            payload = self._read_state_payload()
            self._send_json(payload)
            return

        if self.path.startswith("/api/police/notifications"):
            token = self._parse_bearer()
            ok, username, role = self._verify_token(token)
            if not ok or role != "traffic_police":
                self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                return

            state = self._read_state_payload()
            self._send_json(self._build_police_overview(state, username))
            return

        if self.path.startswith("/api/police/overview"):
            token = self._parse_bearer()
            ok, username, role = self._verify_token(token)
            if not ok or role != "traffic_police":
                self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                return

            state = self._read_state_payload()
            self._send_json(self._build_police_overview(state, username))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/police/login":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json({"ok": False, "error": "invalid json"}, status=400)
                return

            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", "")).strip()
            if username != self.police_user or password != self.police_password:
                self._send_json({"ok": False, "error": "invalid credentials"}, status=401)
                return

            token = self._issue_token(username, "traffic_police")
            self._send_json(
                {
                    "ok": True,
                    "token": token,
                    "user": {"username": username, "role": "traffic_police"},
                    "expires_in": int(self.token_ttl_s),
                }
            )
            return

        if self.path == "/api/user/login":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json({"ok": False, "error": "invalid json"}, status=400)
                return

            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", "")).strip()
            if username != self.app_user or password != self.app_password:
                self._send_json({"ok": False, "error": "invalid credentials"}, status=401)
                return

            token = self._issue_token(username, "user")
            self._send_json(
                {
                    "ok": True,
                    "token": token,
                    "user": {"username": username, "role": "user"},
                    "expires_in": int(self.token_ttl_s),
                }
            )
            return

        if self.path not in {"/api/call", "/api/emergency-call"}:
            if self.path == "/api/call/start-trip":
                token = self._parse_bearer()
                ok, username, role = self._verify_token(token)
                if not ok or role not in {"user", "traffic_police"}:
                    self._send_json({"ok": False, "error": "authentication required"}, status=401)
                    return

                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send_json({"ok": False, "error": "invalid json"}, status=400)
                    return

                call_id = str(payload.get("call_id", "")).strip()
                if not call_id:
                    self._send_json({"ok": False, "error": "call_id required"}, status=400)
                    return

                cmd = {
                    "timestamp": int(time.time()),
                    "action": "start_trip",
                    "call_id": call_id,
                    "requested_by": username,
                    "requested_by_role": role,
                }
                self._append_trip_command(self.trip_command_file, cmd)
                self._send_json({"ok": True, "command": cmd})
                return

            if self.path == "/api/police/reset-ambulances":
                token = self._parse_bearer()
                ok, username, role = self._verify_token(token)
                if not ok or role != "traffic_police":
                    self._send_json({"ok": False, "error": "authentication required"}, status=401)
                    return

                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send_json({"ok": False, "error": "invalid json"}, status=400)
                    return

                ids_raw = payload.get("vehicle_ids", [])
                if isinstance(ids_raw, str):
                    ids_raw = [ids_raw]
                if not isinstance(ids_raw, list):
                    self._send_json({"ok": False, "error": "vehicle_ids must be a list or string"}, status=400)
                    return

                vehicle_ids = [str(v).strip() for v in ids_raw if str(v).strip()]
                cmd = {
                    "timestamp": int(time.time()),
                    "action": "reset_ambulances",
                    "requested_by": username,
                    "requested_by_role": role,
                    "vehicle_ids": vehicle_ids,
                }
                self._append_control_command(self.control_command_file, cmd)
                self._send_json({"ok": True, "command": cmd})
                return

            self._send_json({"ok": False, "error": "not found"}, status=404)
            return

        token = self._parse_bearer()
        ok, username, role = self._verify_token(token)
        if not ok or role not in {"user", "traffic_police"}:
            self._send_json({"ok": False, "error": "authentication required"}, status=401)
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "invalid json"}, status=400)
            return

        try:
            lat = float(payload.get("lat"))
            lon = float(payload.get("lon"))
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "lat/lon required"}, status=400)
            return

        call = {
            "timestamp": int(payload.get("timestamp") or 0) or int(time.time()),
            "call_id": str(payload.get("call_id") or f"call_{int(time.time() * 1000)}"),
            "lat": lat,
            "lon": lon,
            "emergency_type": str(payload.get("emergency_type", "trauma")),
            "caller_name": str(payload.get("caller_name") or username or "citizen"),
            "preferred_hospital_id": str(payload.get("preferred_hospital_id", "")).strip(),
            "created_by": username,
            "created_by_role": role,
            "status": "new",
        }

        self._append_call(self.call_file, call)
        self._send_json({"ok": True, "call": call})


def main() -> None:
    parser = argparse.ArgumentParser(description="Realtime ambulance dashboard server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--state-file", default="out/realtime_state.json")
    parser.add_argument("--index-file", default="web/index.html")
    parser.add_argument("--call-file", default="out/call_requests.jsonl")
    parser.add_argument("--trip-command-file", default="out/trip_commands.jsonl")
    parser.add_argument("--control-command-file", default="out/control_commands.jsonl")
    parser.add_argument("--app-user", default=os.environ.get("APP_USER", "citizen_user"))
    parser.add_argument("--app-password", default=os.environ.get("APP_PASSWORD", "user123"))
    parser.add_argument("--police-user", default=os.environ.get("POLICE_USER", "traffic_police"))
    parser.add_argument("--police-password", default=os.environ.get("POLICE_PASSWORD", "police123"))
    parser.add_argument("--auth-secret", default=os.environ.get("DASHBOARD_AUTH_SECRET", "emergency-command-secret"))
    parser.add_argument("--token-ttl-s", type=int, default=12 * 60 * 60)
    args = parser.parse_args()

    RealtimeHandler.state_file = args.state_file
    RealtimeHandler.index_file = args.index_file
    RealtimeHandler.call_file = args.call_file
    RealtimeHandler.trip_command_file = args.trip_command_file
    RealtimeHandler.control_command_file = args.control_command_file
    RealtimeHandler.app_user = args.app_user
    RealtimeHandler.app_password = args.app_password
    RealtimeHandler.police_user = args.police_user
    RealtimeHandler.police_password = args.police_password
    RealtimeHandler.auth_secret = args.auth_secret
    RealtimeHandler.token_ttl_s = int(args.token_ttl_s)

    httpd = ThreadingHTTPServer((args.host, args.port), RealtimeHandler)
    print(f"Dashboard: http://{args.host}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
