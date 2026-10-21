"""Zorra Terminal: tiny HTTP server for the web dashboard (no extra dependencies).

It only reads the files the bot writes (snapshot, journal, backtest), so it can
run inside the bot process or on its own with `python main.py --ui`."""
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import meme
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


def make_handler(cfg, log=None):
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
            if path == "/api/meme":
                data = read_json(cfg.meme_file, {})
                data["running"] = meme.scan_running()
                data["chains_available"] = {k: v["label"] for k, v in meme.CHAINS.items()}
                return self._send(json.dumps(data, default=str), "application/json")
            return self._send("not found", "text/plain", 404)

        def _json_body(self):
            # browsers cannot send application/json cross-origin without a CORS preflight,
            # which this server never approves, so other websites cannot trigger these calls
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("Content-Type harus application/json")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                raise ValueError("request terlalu besar")
            return json.loads(self.rfile.read(length) or b"{}")

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                body = self._json_body()
            except ValueError as exc:
                return self._send(json.dumps({"error": str(exc)}), "application/json", 400)
            if path == "/api/meme/scan":
                chains = [c for c in body.get("chains") or cfg.meme_chains if c in meme.CHAINS]
                started = meme.start_scan_async(cfg, chains or None, log)
                return self._send(json.dumps({"started": started, "running": True}), "application/json")
            if path == "/api/meme/check":
                try:
                    result = meme.screen_token(cfg, str(body.get("chain", "")), str(body.get("address", "")).strip())
                except ValueError as exc:
                    return self._send(json.dumps({"error": str(exc)}), "application/json", 400)
                except Exception as exc:  # upstream API failure
                    return self._send(json.dumps({"error": f"gagal mengambil data: {exc}"}), "application/json", 502)
                return self._send(json.dumps(result, default=str), "application/json")
            return self._send("not found", "text/plain", 404)

    return Handler


def serve(cfg, log=None, background=True):
    server = ThreadingHTTPServer((cfg.ui_host, cfg.ui_port), make_handler(cfg, log))
    if log:
        log.info(f"Zorra Terminal on http://{cfg.ui_host}:{cfg.ui_port}")
    if background:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    server.serve_forever()
