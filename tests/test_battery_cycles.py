import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import CYCLES_ANALYZE_ROUTE, HEALTH_ROUTE, Handler
from powerrail.service import ApiError, Service


def base_request(**overrides):
    payload = {
        "samples": [
            {"timestamp_s": 0, "soc": 0.2},
            {"timestamp_s": 100, "soc": 0.9},
            {"timestamp_s": 200, "soc": 0.4},
            {"timestamp_s": 300, "soc": 0.7},
            {"timestamp_s": 400, "soc": 0.2},
        ],
        "cycle_life_curve": [
            {"depth": 0.2, "cycles_to_eol": 10000.0},
            {"depth": 0.8, "cycles_to_eol": 2000.0},
        ],
    }
    payload.update(overrides)
    return payload


class CycleAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_closed_cycle_and_residue_in_identification_order(self) -> None:
        result = self.service.analyze_battery_cycles(base_request())
        cycles = result["cycles"]
        self.assertEqual(len(cycles), 3)
        # Closed 0.4<->0.7 loop counts 1, then the 0.2<->0.9 residue
        # contributes two half counts.
        self.assertAlmostEqual(cycles[0]["depth"], 0.3)
        self.assertAlmostEqual(cycles[0]["count"], 1.0)
        self.assertAlmostEqual(cycles[1]["depth"], 0.7)
        self.assertAlmostEqual(cycles[1]["count"], 0.5)
        self.assertAlmostEqual(cycles[2]["depth"], 0.7)
        self.assertAlmostEqual(cycles[2]["count"], 0.5)
        self.assertAlmostEqual(result["cycle_count"], 2.0)
        self.assertAlmostEqual(result["equivalent_full_cycles"], 1.0)

    def test_damage_uses_interpolated_cycle_life(self) -> None:
        result = self.service.analyze_battery_cycles(base_request())
        cycles = result["cycles"]
        # depth 0.3 -> 10000 + (0.1/0.6) * (2000-10000) = 8666.66...
        self.assertAlmostEqual(cycles[0]["cycles_to_eol"], 8666.666666666667)
        self.assertAlmostEqual(cycles[0]["damage"], 1.0 / 8666.666666666667)
        # depth 0.7 -> 10000 + (0.5/0.6) * (2000-10000) = 3333.33...
        self.assertAlmostEqual(cycles[1]["cycles_to_eol"], 3333.3333333333335)
        self.assertAlmostEqual(cycles[1]["damage"], 0.5 / 3333.3333333333335)
        expected_total = 1.0 / 8666.666666666667 + 2.0 * (0.5 / 3333.3333333333335)
        self.assertAlmostEqual(result["total_damage"], expected_total)
        self.assertAlmostEqual(result["remaining_life_ratio"], 1.0 - expected_total)
        self.assertEqual(result["status"], "active")

    def test_simple_swing_yields_two_half_cycles(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.2},
                {"timestamp_s": 50, "soc": 0.8},
                {"timestamp_s": 100, "soc": 0.2},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(len(result["cycles"]), 2)
        for cycle in result["cycles"]:
            self.assertAlmostEqual(cycle["depth"], 0.6)
            self.assertAlmostEqual(cycle["count"], 0.5)
        self.assertAlmostEqual(result["cycle_count"], 1.0)
        self.assertAlmostEqual(result["equivalent_full_cycles"], 0.6)

    def test_adjacent_duplicate_soc_values_collapse(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.5},
                {"timestamp_s": 10, "soc": 0.5},
                {"timestamp_s": 20, "soc": 0.8},
                {"timestamp_s": 30, "soc": 0.8},
                {"timestamp_s": 40, "soc": 0.5},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(len(result["cycles"]), 2)
        for cycle in result["cycles"]:
            self.assertAlmostEqual(cycle["depth"], 0.3)
            self.assertAlmostEqual(cycle["count"], 0.5)

    def test_monotonic_history_counts_single_residual_range(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.1},
                {"timestamp_s": 10, "soc": 0.4},
                {"timestamp_s": 20, "soc": 0.9},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(len(result["cycles"]), 1)
        self.assertAlmostEqual(result["cycles"][0]["depth"], 0.8)
        self.assertAlmostEqual(result["cycles"][0]["count"], 0.5)

    def test_min_cycle_depth_filters_shallow_cycles(self) -> None:
        result = self.service.analyze_battery_cycles(
            base_request(min_cycle_depth=0.5)
        )
        self.assertEqual(len(result["cycles"]), 2)
        for cycle in result["cycles"]:
            self.assertAlmostEqual(cycle["depth"], 0.7)
        self.assertAlmostEqual(result["cycle_count"], 1.0)
        self.assertAlmostEqual(result["equivalent_full_cycles"], 0.7)

    def test_cycle_at_exactly_min_cycle_depth_is_kept(self) -> None:
        payload = base_request(
            min_cycle_depth=0.5,
            samples=[
                {"timestamp_s": 0, "soc": 0.25},
                {"timestamp_s": 10, "soc": 0.75},
                {"timestamp_s": 20, "soc": 0.25},
            ],
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(len(result["cycles"]), 2)
        for cycle in result["cycles"]:
            self.assertAlmostEqual(cycle["depth"], 0.5)

    def test_min_cycle_depth_default_is_zero(self) -> None:
        result = self.service.analyze_battery_cycles(base_request())
        self.assertEqual(len(result["cycles"]), 3)

    def test_depth_outside_curve_clamps_to_nearest_endpoint(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.9},
                {"timestamp_s": 10, "soc": 1.0},
                {"timestamp_s": 20, "soc": 0.9},
            ],
            cycle_life_curve=[
                {"depth": 0.5, "cycles_to_eol": 500.0},
                {"depth": 1.0, "cycles_to_eol": 100.0},
            ],
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(len(result["cycles"]), 2)
        for cycle in result["cycles"]:
            self.assertAlmostEqual(cycle["depth"], 0.1)
            self.assertAlmostEqual(cycle["cycles_to_eol"], 500.0)
            self.assertAlmostEqual(cycle["damage"], 0.5 / 500.0)

    def test_exhausted_status_when_damage_reaches_one(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.0},
                {"timestamp_s": 10, "soc": 1.0},
                {"timestamp_s": 20, "soc": 0.0},
            ],
            cycle_life_curve=[
                {"depth": 0.5, "cycles_to_eol": 2.0},
                {"depth": 1.0, "cycles_to_eol": 1.0},
            ],
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertAlmostEqual(result["total_damage"], 1.0)
        self.assertEqual(result["remaining_life_ratio"], 0.0)
        self.assertEqual(result["status"], "exhausted")

    def test_constant_soc_yields_no_cycles(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.5},
                {"timestamp_s": 10, "soc": 0.5},
                {"timestamp_s": 20, "soc": 0.5},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(result["cycles"], [])
        self.assertEqual(result["cycle_count"], 0.0)
        self.assertEqual(result["equivalent_full_cycles"], 0.0)
        self.assertEqual(result["total_damage"], 0.0)
        self.assertEqual(result["remaining_life_ratio"], 1.0)
        self.assertEqual(result["status"], "active")

    def test_filtering_every_cycle_yields_zero_summary(self) -> None:
        result = self.service.analyze_battery_cycles(
            base_request(min_cycle_depth=1.0)
        )
        self.assertEqual(result["cycles"], [])
        self.assertEqual(result["cycle_count"], 0.0)
        self.assertEqual(result["equivalent_full_cycles"], 0.0)
        self.assertEqual(result["total_damage"], 0.0)
        self.assertEqual(result["remaining_life_ratio"], 1.0)
        self.assertEqual(result["status"], "active")

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request(min_cycle_depth=0.1)
        snapshot = json.loads(json.dumps(payload))
        self.service.analyze_battery_cycles(payload)
        self.assertEqual(payload, snapshot)


class CycleAnalysisValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.analyze_battery_cycles(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_samples_rules(self) -> None:
        self.assert_code(base_request(samples=[]), "invalid_samples")
        self.assert_code(
            base_request(samples=[{"timestamp_s": 0, "soc": 0.5}]),
            "invalid_samples",
        )
        self.assert_code(base_request(samples="x"), "invalid_samples")
        self.assert_code(base_request(samples=None), "invalid_samples")
        self.assert_code(base_request(samples=[1, 2]), "invalid_samples")
        payload = base_request()
        del payload["samples"]
        self.assert_code(payload, "invalid_samples")

    def test_timestamp_rules(self) -> None:
        missing = [
            {"soc": 0.2},
            {"timestamp_s": 10, "soc": 0.8},
        ]
        self.assert_code(base_request(samples=missing), "invalid_timestamp")
        non_finite = [
            {"timestamp_s": 0, "soc": 0.2},
            {"timestamp_s": float("nan"), "soc": 0.8},
        ]
        self.assert_code(base_request(samples=non_finite), "invalid_timestamp")
        not_increasing = [
            {"timestamp_s": 10, "soc": 0.2},
            {"timestamp_s": 10, "soc": 0.8},
        ]
        self.assert_code(base_request(samples=not_increasing), "invalid_timestamp")
        boolean = [
            {"timestamp_s": 0, "soc": 0.2},
            {"timestamp_s": True, "soc": 0.8},
        ]
        self.assert_code(base_request(samples=boolean), "invalid_timestamp")

    def test_soc_rules(self) -> None:
        for bad in (-0.01, 1.01, float("nan"), "x", True, None):
            samples = [
                {"timestamp_s": 0, "soc": 0.2},
                {"timestamp_s": 10, "soc": bad},
            ]
            self.assert_code(base_request(samples=samples), "invalid_soc")

    def test_cycle_life_curve_rules(self) -> None:
        self.assert_code(
            base_request(cycle_life_curve=[]), "invalid_cycle_life_curve"
        )
        self.assert_code(
            base_request(
                cycle_life_curve=[{"depth": 0.5, "cycles_to_eol": 100.0}]
            ),
            "invalid_cycle_life_curve",
        )
        self.assert_code(
            base_request(cycle_life_curve="x"), "invalid_cycle_life_curve"
        )
        self.assert_code(
            base_request(cycle_life_curve=[1, 2]), "invalid_cycle_life_curve"
        )
        payload = base_request()
        del payload["cycle_life_curve"]
        self.assert_code(payload, "invalid_cycle_life_curve")
        for bad_depth in (0, -0.1, 1.01, float("nan"), "x", True):
            curve = [
                {"depth": bad_depth, "cycles_to_eol": 100.0},
                {"depth": 0.8, "cycles_to_eol": 50.0},
            ]
            self.assert_code(
                base_request(cycle_life_curve=curve), "invalid_cycle_life_curve"
            )
        not_increasing = [
            {"depth": 0.5, "cycles_to_eol": 100.0},
            {"depth": 0.5, "cycles_to_eol": 50.0},
        ]
        self.assert_code(
            base_request(cycle_life_curve=not_increasing),
            "invalid_cycle_life_curve",
        )
        for bad_cycles in (0, -1.0, float("inf"), "x", False):
            curve = [
                {"depth": 0.2, "cycles_to_eol": 100.0},
                {"depth": 0.8, "cycles_to_eol": bad_cycles},
            ]
            self.assert_code(
                base_request(cycle_life_curve=curve), "invalid_cycle_life_curve"
            )
        increasing_life = [
            {"depth": 0.2, "cycles_to_eol": 50.0},
            {"depth": 0.8, "cycles_to_eol": 100.0},
        ]
        self.assert_code(
            base_request(cycle_life_curve=increasing_life),
            "invalid_cycle_life_curve",
        )
        # Equal consecutive cycles_to_eol is allowed: non-increasing.
        flat = [
            {"depth": 0.2, "cycles_to_eol": 100.0},
            {"depth": 0.8, "cycles_to_eol": 100.0},
        ]
        result = self.service.analyze_battery_cycles(
            base_request(cycle_life_curve=flat)
        )
        self.assertEqual(result["status"], "active")

    def test_min_cycle_depth_rules(self) -> None:
        for bad in (-0.01, 1.01, float("nan"), "x", True):
            self.assert_code(
                base_request(min_cycle_depth=bad), "invalid_options"
            )

    def test_validation_order_samples_before_curve(self) -> None:
        payload = base_request(samples=[], cycle_life_curve=[])
        self.assert_code(payload, "invalid_samples")


class CycleAnalysisHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def post(self, route, raw, *, content_type="application/json"):
        request = urllib.request.Request(
            self.base + route,
            data=raw,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read())
            exc.close()
            return exc.code, body

    def test_cycles_analyze_route_returns_200(self) -> None:
        status, body = self.post(CYCLES_ANALYZE_ROUTE, json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "active")
        self.assertAlmostEqual(body["cycle_count"], 2.0)

    def test_http_result_matches_service_result(self) -> None:
        payload = base_request()
        status, body = self.post(CYCLES_ANALYZE_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 200)
        expected = Service().analyze_battery_cycles(payload)
        self.assertEqual(body, json.loads(json.dumps(expected)))

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(CYCLES_ANALYZE_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(CYCLES_ANALYZE_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(CYCLES_ANALYZE_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_soc_is_422(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.2},
                {"timestamp_s": 10, "soc": 1.5},
            ]
        )
        status, body = self.post(CYCLES_ANALYZE_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_soc")
        self.assertTrue(body["error"]["message"])

    def test_invalid_cycle_life_curve_is_422(self) -> None:
        payload = base_request(cycle_life_curve=[])
        status, body = self.post(CYCLES_ANALYZE_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_cycle_life_curve")

    def test_unknown_post_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/cycles/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_health_route_still_works(self) -> None:
        payload = {
            "nominal_capacity_ah": 100.0,
            "measurements": [
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 100.0,
                    "cumulative_discharge_ah": 0.0,
                },
                {
                    "timestamp_s": 100,
                    "measured_capacity_ah": 90.0,
                    "cumulative_discharge_ah": 100.0,
                },
            ],
        }
        status, body = self.post(HEALTH_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["projection_status"], "projected")

    def test_healthz_get_still_works(self) -> None:
        with urllib.request.urlopen(self.base + "/healthz") as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
