"""HTTP entry point for PowerRail."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import ApiError, Service

SOC_ROUTE = "/v1/battery/soc/estimate"
HEALTH_ROUTE = "/v1/battery/health/estimate"


def env_address() -> tuple[str, int]:
    raw = os.environ.get("POWERRAIL_ADDR", "127.0.0.1:8080")
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid POWERRAIL_ADDR: {raw!r}")
    return host, int(port)


class Handler(BaseHTTPRequestHandler):
    service = Service()

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, self.service.health())
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def do_POST(self) -> None:
        if self.path not in (SOC_ROUTE, HEALTH_ROUTE):
            self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(
                400, {"error": {"code": "invalid_json", "message": "request body must be valid JSON"}}
            )
            return
        if self.path == HEALTH_ROUTE:
            handler = self.service.estimate_health
        else:
            handler = self.service.estimate_soc
        try:
            result = handler(payload)
        except ApiError as exc:
            self.send_json(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
            return
        self.send_json(200, result)

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence per-request logging so recorded output stays stable."""


def main() -> int:
    parser = argparse.ArgumentParser(prog="powerrail.server", description="电源与能耗管理平台")
    host, port = env_address()
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"PowerRail listening on http://{args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
