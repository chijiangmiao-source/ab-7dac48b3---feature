"""HTTP API + static page for the deep-space frame receiver.

Endpoints
    GET  /                       static single-page UI
    GET  /healthz                liveness probe -> 200 {"status":"ok"}
    GET  /api/links              list links with window state
    POST /api/links              {"name": ...} -> create link
    GET  /api/links/{id}         link state (highest extended seq + bitmap)
    POST /api/links/{id}/frames   submit an arriving frame

Frame body: {"counter": <u32>, "receipt_id": "...", "payload": ...}
Verdict:    {"status": "accepted|duplicate|expired|rejected", ...}
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .db import Database, ReceiptConflict

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
COUNTER_MAX = (1 << 32) - 1


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepSpaceRX/1.0"

    def log_message(self, fmt, *args):
        # Concise one-line access log.
        self.server.log_line("%s - %s" % (self.address_string(), fmt % args))

    # ----------------------------------------------------------------- utils

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise _BadRequest("body must be valid JSON")
        if not isinstance(data, dict):
            raise _BadRequest("body must be a JSON object")
        return data

    def _db(self):
        return self.server.db

    # ---------------------------------------------------------------- routing

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif path == "/api/links":
            self._send_json(200, {"links": self._db().list_links()})
        elif path.startswith("/api/links/"):
            link_id = path.split("/")[3]
            link = self._db().get_link(link_id)
            if link is None:
                self._send_json(404, {"error": "unknown link"})
            else:
                self._send_json(200, link)
        elif path in ("/", "/index.html"):
            self._serve_index()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/links":
                data = self._read_json()
                name = str(data.get("name") or "link")
                link = self._db().create_link(name[:128])
                self._send_json(201, link)
            elif path.startswith("/api/links/") and path.endswith("/frames"):
                self._submit_frame(path.split("/")[3])
            else:
                self._send_json(404, {"error": "not found"})
        except _BadRequest as exc:
            self._send_json(400, {"error": str(exc)})
        except ReceiptConflict:
            self._send_json(409, {"error": "receipt id reused with a different "
                                           "link, counter or payload"})
        except LookupError:
            self._send_json(404, {"error": "unknown link"})

    def _submit_frame(self, link_id):
        data = self._read_json()
        if "counter" not in data:
            raise _BadRequest("counter is required")
        if "receipt_id" not in data or not str(data["receipt_id"]).strip():
            raise _BadRequest("receipt_id is required")
        try:
            counter = int(data["counter"])
        except (TypeError, ValueError):
            raise _BadRequest("counter must be an integer")
        if not 0 <= counter <= COUNTER_MAX:
            raise _BadRequest("counter must be an unsigned 32-bit integer")
        payload = data.get("payload")
        verdict = self._db().submit_frame(
            link_id, counter, str(data["receipt_id"]), payload
        )
        self._send_json(200, verdict)

    def _serve_index(self):
        try:
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                body = fh.read()
        except OSError:
            self._send_json(500, {"error": "page missing"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _BadRequest(Exception):
    pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, db_path, log_file=None):
        import sys
        super().__init__(addr, Handler)
        self.db = Database(db_path)
        self._log = log_file if log_file is not None else sys.stderr

    def log_line(self, line):
        print(line, file=self._log, flush=True)


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("DB_PATH", "/data/receiver.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    server = Server((host, port), db_path)
    print(f"deep-space receiver listening on {host}:{port} (db {db_path})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.db.close()


if __name__ == "__main__":
    main()
