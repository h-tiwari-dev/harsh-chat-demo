"""python -m demo.server  ->  http://127.0.0.1:8765  (stdlib only)"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .session import SCENARIOS, Session

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
S = {"session": None}


def current(name=None):
    if name or S["session"] is None:
        if S["session"]:
            S["session"].close()
        S["session"] = Session(name or "happy")
    return S["session"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/":
            return self._send(open(os.path.join(HERE, "index.html"), "rb").read(), ctype="text/html; charset=utf-8")
        if path == "/api/scenarios":
            return self._send([{"id": k, "label": v[0], "suggest": v[2]} for k, v in SCENARIOS.items()])
        if path == "/api/trace":
            since = int(dict(p.split("=") for p in qs.split("&") if "=" in p).get("since", 0))
            with LOCK:
                s = current()
                return self._send({"events": s.trace(since), "phase": s.phase, "scenario": s.name,
                                   "restarts": s.restarts, "trace_path": s.trace_path, "run_id": s.run_id})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        with LOCK:
            try:
                if self.path == "/api/reset":
                    s = current(data.get("scenario", "happy"))
                    return self._send({"ok": True, "scenario": s.name, "trace_path": s.trace_path})
                if self.path == "/api/chat":
                    return self._send(current().send(str(data.get("text", ""))))
                if self.path == "/api/restart":
                    return self._send(current().restart())
            except Exception as e:
                return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send({"error": "not found"}, 404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 8765), H)
    print("Errand demo viewer: http://127.0.0.1:8765")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
