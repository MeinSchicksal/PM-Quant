"""PM-Quant Lightweight Web Dashboard Server.

Provides zero-dependency HTTP server and REST APIs for real-time monitoring.
"""
import sys
import json
import time
import mimetypes
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config

STATIC_DIR = config.ROOT / "web" / "static"


class QuantHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence routine HTTP access logging to keep terminal output clean
        pass

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self.send_json({
                "status": "ok",
                "version": config.VERSION,
                "revision": config.REVISION,
                "batch_id": config.BATCH_ID,
                "time": time.time()
            })
        elif path == "/api/live":
            live_file = config.CURRENT_LEDGER_DIR / "live.json"
            if live_file.exists():
                try:
                    data = json.loads(live_file.read_text(encoding="utf-8"))
                    self.send_json(data)
                    return
                except Exception as e:
                    self.send_json({"error": str(e), "batch_id": config.BATCH_ID}, status=500)
                    return
            self.send_json({"status": "idle", "batch_id": config.BATCH_ID, "message": "No active window"})
        elif path == "/api/history":
            qs = parse_qs(parsed.query)
            target_batch = qs.get("batch", [config.BATCH_ID])[0]
            ledger_dir = config.LEDGERS_DIR / target_batch
            summary_file = ledger_dir / "summary.jsonl"
            
            windows = []
            if summary_file.exists():
                try:
                    for line in summary_file.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            windows.append(json.loads(line))
                except Exception:
                    pass

            total_pnl = sum(w.get("net_pnl", 0.0) for w in windows)
            wins = sum(1 for w in windows if w.get("net_pnl", 0.0) > 0)
            losses = sum(1 for w in windows if w.get("net_pnl", 0.0) < 0)
            win_rate = (wins / len(windows)) * 100 if windows else 0.0
            avg_pnl = (total_pnl / len(windows)) if windows else 0.0

            self.send_json({
                "batch_id": target_batch,
                "total_windows": len(windows),
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 1),
                "total_pnl": round(total_pnl, 4),
                "avg_pnl": round(avg_pnl, 4),
                "windows": list(reversed(windows))[:50]  # latest 50 windows
            })
        elif path == "/api/batches":
            batches = [p.name for p in config.LEDGERS_DIR.iterdir() if p.is_dir()]
            self.send_json({"batches": sorted(batches, reverse=True)})
        else:
            # Serve static files
            rel_path = path.lstrip("/")
            if not rel_path or rel_path == "index.html":
                file_path = STATIC_DIR / "index.html"
            else:
                file_path = STATIC_DIR / rel_path

            if file_path.exists() and file_path.is_file():
                mime, _ = mimetypes.guess_type(str(file_path))
                mime = mime or "application/octet-stream"
                body = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", f"{mime}; charset=utf-8" if "text" in mime or "javascript" in mime else mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"404 Not Found")


def run(port: int = 8900):
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, QuantHandler)
    print(f"PM-Quant Web Server running at http://localhost:{port}/ (Batch: {config.BATCH_ID})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        httpd.server_close()


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8900
    run(p)
