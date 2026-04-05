import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RealtimeHandler(BaseHTTPRequestHandler):
    state_file = "out/realtime_state.json"
    index_file = "web/index.html"

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

    def do_GET(self):
        if self.path in {"/", "/index.html"}:
            if not os.path.exists(self.index_file):
                self._send_html("<h1>index.html not found</h1>", status=404)
                return
            with open(self.index_file, "r", encoding="utf-8") as f:
                self._send_html(f.read())
            return

        if self.path.startswith("/api/state"):
            if not os.path.exists(self.state_file):
                self._send_json({"timestamp": 0, "active_ambulances": 0, "ambulances": []})
                return
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                payload = {"timestamp": 0, "active_ambulances": 0, "ambulances": []}
            self._send_json(payload)
            return

        self.send_response(404)
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="Realtime ambulance dashboard server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--state-file", default="out/realtime_state.json")
    parser.add_argument("--index-file", default="web/index.html")
    args = parser.parse_args()

    RealtimeHandler.state_file = args.state_file
    RealtimeHandler.index_file = args.index_file

    httpd = ThreadingHTTPServer((args.host, args.port), RealtimeHandler)
    print(f"Dashboard: http://{args.host}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
