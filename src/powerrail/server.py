"""HTTP entry point for PowerRail."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import ApiError, Service

SOC_ROUTE = "/v1/battery/soc/estimate"
HEALTH_ROUTE = "/v1/battery/health/estimate"
POWER_BUDGET_ROUTE = "/v1/power/budget/allocate"
LOAD_SHED_ROUTE = "/v1/power/load-shed/decide"
DCDC_EFFICIENCY_ROUTE = "/v1/power/dcdc/efficiency/estimate"
LDO_EFFICIENCY_ROUTE = "/v1/power/ldo/efficiency/estimate"
TELEMETRY_FILTER_ROUTE = "/v1/telemetry/filter"
TELEMETRY_AGGREGATE_ROUTE = "/v1/telemetry/aggregate"
THERMAL_PROTECT_ROUTE = "/v1/battery/thermal/protect"
BALANCE_PLAN_ROUTE = "/v1/battery/balance/plan"
CHARGE_PLAN_ROUTE = "/v1/battery/charge/plan"
PARALLEL_DISPATCH_ROUTE = "/v1/battery/packs/parallel/dispatch"
SOLAR_HARVEST_ROUTE = "/v1/energy/solar/harvest/estimate"
WIRELESS_NEGOTIATE_ROUTE = "/v1/power/wireless/negotiate"
ENERGY_BENCHMARK_COMPARE_ROUTE = "/v1/energy/benchmark/compare"
BATTERY_MODEL_SIMULATE_ROUTE = "/v1/battery/model/simulate"


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
        if self.path not in (
            SOC_ROUTE,
            HEALTH_ROUTE,
            POWER_BUDGET_ROUTE,
            LOAD_SHED_ROUTE,
            DCDC_EFFICIENCY_ROUTE,
            LDO_EFFICIENCY_ROUTE,
            TELEMETRY_FILTER_ROUTE,
            TELEMETRY_AGGREGATE_ROUTE,
            THERMAL_PROTECT_ROUTE,
            BALANCE_PLAN_ROUTE,
            CHARGE_PLAN_ROUTE,
            PARALLEL_DISPATCH_ROUTE,
            SOLAR_HARVEST_ROUTE,
            WIRELESS_NEGOTIATE_ROUTE,
            ENERGY_BENCHMARK_COMPARE_ROUTE,
            BATTERY_MODEL_SIMULATE_ROUTE,
        ):
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
        try:
            if self.path == SOC_ROUTE:
                result = self.service.estimate_soc(payload)
            elif self.path == HEALTH_ROUTE:
                result = self.service.estimate_health(payload)
            elif self.path == POWER_BUDGET_ROUTE:
                result = self.service.allocate_power_budget(payload)
            elif self.path == LOAD_SHED_ROUTE:
                result = self.service.decide_load_shedding(payload)
            elif self.path == DCDC_EFFICIENCY_ROUTE:
                result = self.service.estimate_dcdc_efficiency(payload)
            elif self.path == LDO_EFFICIENCY_ROUTE:
                result = self.service.estimate_ldo_efficiency(payload)
            elif self.path == THERMAL_PROTECT_ROUTE:
                result = self.service.protect_thermal(payload)
            elif self.path == BALANCE_PLAN_ROUTE:
                result = self.service.plan_balance(payload)
            elif self.path == CHARGE_PLAN_ROUTE:
                result = self.service.plan_charging(payload)
            elif self.path == PARALLEL_DISPATCH_ROUTE:
                result = self.service.dispatch_parallel_packs(payload)
            elif self.path == SOLAR_HARVEST_ROUTE:
                result = self.service.estimate_solar_harvest(payload)
            elif self.path == WIRELESS_NEGOTIATE_ROUTE:
                result = self.service.negotiate_wireless_charging(payload)
            elif self.path == ENERGY_BENCHMARK_COMPARE_ROUTE:
                result = self.service.compare_energy_benchmark(payload)
            elif self.path == BATTERY_MODEL_SIMULATE_ROUTE:
                result = self.service.simulate_battery_model(payload)
            elif self.path == TELEMETRY_AGGREGATE_ROUTE:
                result = self.service.aggregate_telemetry(payload)
            else:
                result = self.service.filter_telemetry(payload)
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
