"""Zorra Terminal: tiny HTTP server for the web dashboard (no extra dependencies).

It only reads the files the bot writes (snapshot, journal, backtest), so it can
run inside the bot process or on its own with `python main.py --ui`."""
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .journal import read_json, stats, to_csv

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def build_state(cfg):
    journal = read_json(cfg.journal_file, [])
    backtest = read_json(cfg.backtest_file, None)
    snap = read_json(cfg.snapshot_file, {})
    if backtest:
        backtest["stats"] = stats(backtest.get("records", []))
    return {
        "snapshot": snap,
        "journal": journal[-500:],
        "stats": stats(journal),
        "backtest": backtest,
    }


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the bot log clean
            pass

        def _send(self, body, ctype, status=200, extra=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(UI_DIR, "index.html"), "rb") as f:
                    return self._send(f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(json.dumps(build_state(cfg), default=str), "application/json")
            if path in ("/api/journal.csv", "/api/backtest.csv"):
                if path == "/api/journal.csv":
                    records = read_json(cfg.journal_file, [])
                else:
                    records = (read_json(cfg.backtest_file, None) or {}).get("records", [])
                buf = io.StringIO()
                to_csv(records, buf)
                name = "zorra-signals.csv" if "journal" in path else "zorra-backtest.csv"
                return self._send(buf.getvalue(), "text/csv",
                                  extra={"Content-Disposition": f"attachment; filename={name}"})
            return self._send("not found", "text/plain", 404)

    return Handler


def serve(cfg, log=None, background=True):
    server = ThreadingHTTPServer((cfg.ui_host, cfg.ui_port), make_handler(cfg))
    if log:
        log.info(f"Zorra Terminal on http://{cfg.ui_host}:{cfg.ui_port}")
    if background:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    server.serve_forever()
