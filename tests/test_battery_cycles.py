import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import BATTERY_CYCLES_ROUTE, Handler
from powerrail.service import ApiError, Service


def life_curve():
    return [
        {"depth": 0.1, "cycles_to_eol": 10000.0},
        {"depth": 0.5, "cycles_to_eol": 2000.0},
        {"depth": 1.0, "cycles_to_eol": 500.0},
    ]


def base_request(**overrides):
    payload = {
        "samples": [
            {"timestamp_s": 0, "soc": 0.0},
            {"timestamp_s": 100, "soc": 1.0},
            {"timestamp_s": 200, "soc": 0.0},
        ],
        "cycle_life_curve": life_curve(),
    }
    payload.update(overrides)
    return payload


class BatteryCyclesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_single_full_excursion_counts_two_half_ranges(self) -> None:
        result = self.service.analyze_battery_cycles(base_request())
        cycles = result["cycles"]
        self.assertEqual(len(cycles), 2)
        for cycle in cycles:
            self.assertAlmostEqual(cycle["depth"], 1.0)
            self.assertAlmostEqual(cycle["count"], 0.5)
            self.assertAlmostEqual(cycle["cycles_to_eol"], 500.0)
            self.assertAlmostEqual(cycle["damage"], 0.001)
        self.assertAlmostEqual(result["cycle_count"], 1.0)
        self.assertAlmostEqual(result["equivalent_full_cycles"], 1.0)
        self.assertAlmostEqual(result["total_damage"], 0.002)
        self.assertAlmostEqual(result["remaining_life_ratio"], 0.998)
        self.assertEqual(result["status"], "active")

    def test_nested_cycle_counts_inner_full_outer_halves(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.2},
                {"timestamp_s": 1, "soc": 0.8},
                {"timestamp_s": 2, "soc": 0.5},
                {"timestamp_s": 3, "soc": 0.8},
                {"timestamp_s": 4, "soc": 0.2},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        cycles = result["cycles"]
        # Inner closed cycle depth 0.3, outer residue depth 0.6 twice.
        self.assertAlmostEqual(cycles[0]["depth"], 0.3)
        self.assertAlmostEqual(cycles[0]["count"], 1.0)
        self.assertAlmostEqual(cycles[0]["cycles_to_eol"], 6000.0)
        self.assertAlmostEqual(cycles[0]["damage"], 1.0 / 6000.0)
        self.assertAlmostEqual(cycles[1]["depth"], 0.6)
        self.assertAlmostEqual(cycles[1]["count"], 0.5)
        self.assertAlmostEqual(cycles[2]["depth"], 0.6)
        self.assertAlmostEqual(cycles[2]["count"], 0.5)
        self.assertAlmostEqual(result["cycle_count"], 2.0)
        self.assertAlmostEqual(result["equivalent_full_cycles"], 0.9)

    def test_equal_adjacent_soc_is_compressed_to_reversal(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.0},
                {"timestamp_s": 1, "soc": 0.5},
                {"timestamp_s": 2, "soc": 0.5},
                {"timestamp_s": 3, "soc": 1.0},
                {"timestamp_s": 4, "soc": 1.0},
                {"timestamp_s": 5, "soc": 0.0},
            ]
        )
        # Compressed turning series 0 -> 1 -> 0: two half ranges of depth 1.
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(len(result["cycles"]), 2)
        self.assertAlmostEqual(result["cycle_count"], 1.0)

    def test_monotonic_history_leaves_one_half_range(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.3},
                {"timestamp_s": 1, "soc": 0.3},
                {"timestamp_s": 2, "soc": 0.5},
                {"timestamp_s": 3, "soc": 0.7},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(len(result["cycles"]), 1)
        self.assertAlmostEqual(result["cycles"][0]["depth"], 0.4)
        self.assertAlmostEqual(result["cycles"][0]["count"], 0.5)
        self.assertAlmostEqual(result["cycle_count"], 0.5)

    def test_flat_history_has_no_cycles(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.4},
                {"timestamp_s": 1, "soc": 0.4},
                {"timestamp_s": 2, "soc": 0.4},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(result["cycles"], [])
        self.assertEqual(result["cycle_count"], 0)
        self.assertEqual(result["equivalent_full_cycles"], 0)
        self.assertEqual(result["total_damage"], 0)
        self.assertEqual(result["remaining_life_ratio"], 1.0)
        self.assertEqual(result["status"], "active")

    def test_min_cycle_depth_filters_shallow_cycles(self) -> None:
        payload = base_request(min_cycle_depth=0.8)
        result = self.service.analyze_battery_cycles(payload)
        # depth 1.0 is kept (strictly-less rule is not triggered).
        self.assertEqual(len(result["cycles"]), 2)

        payload = base_request(
            min_cycle_depth=1.0,
            samples=[
                {"timestamp_s": 0, "soc": 0.2},
                {"timestamp_s": 1, "soc": 0.8},
                {"timestamp_s": 2, "soc": 0.2},
            ],
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(result["cycles"], [])
        self.assertEqual(result["total_damage"], 0)
        self.assertEqual(result["status"], "active")

    def test_interpolation_clamps_to_nearest_endpoint(self) -> None:
        # Depth 0.05 below the first knot (0.1) resolves to 10000 cycles.
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.45},
                {"timestamp_s": 1, "soc": 0.5},
                {"timestamp_s": 2, "soc": 0.45},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        self.assertAlmostEqual(result["cycles"][0]["cycles_to_eol"], 10000.0)
        # Depth 0.3 lies halfway between 0.1 (10000) and 0.5 (2000).
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "soc": 0.35},
                {"timestamp_s": 1, "soc": 0.65},
                {"timestamp_s": 2, "soc": 0.35},
            ]
        )
        result = self.service.analyze_battery_cycles(payload)
        for cycle in result["cycles"]:
            self.assertAlmostEqual(cycle["cycles_to_eol"], 6000.0)

    def test_exhausted_status_at_full_damage(self) -> None:
        curve = [
            {"depth": 0.5, "cycles_to_eol": 1.0},
            {"depth": 1.0, "cycles_to_eol": 1.0},
        ]
        payload = base_request(cycle_life_curve=curve)
        result = self.service.analyze_battery_cycles(payload)
        self.assertAlmostEqual(result["total_damage"], 1.0)
        self.assertEqual(result["remaining_life_ratio"], 0.0)
        self.assertEqual(result["status"], "exhausted")

    def test_remaining_life_ratio_floored_at_zero(self) -> None:
        curve = [
            {"depth": 0.5, "cycles_to_eol": 0.4},
            {"depth": 1.0, "cycles_to_eol": 0.4},
        ]
        payload = base_request(cycle_life_curve=curve)
        result = self.service.analyze_battery_cycles(payload)
        self.assertEqual(result["remaining_life_ratio"], 0.0)
        self.assertEqual(result["status"], "exhausted")

    def test_default_min_cycle_depth_is_zero(self) -> None:
        payload = base_request()
        self.assertNotIn("min_cycle_depth", payload)
        result = self.service.analyze_battery_cycles(payload)
        self.assertTrue(result["cycles"])

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.analyze_battery_cycles(payload)
        self.assertEqual(payload, snapshot)


class BatteryCyclesValidationTest(unittest.TestCase):
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
        self.assert_code(
            {k: v for k, v in base_request().items() if k != "samples"},
            "invalid_samples",
        )
        self.assert_code(base_request(samples=[]), "invalid_samples")
        self.assert_code(base_request(samples="x"), "invalid_samples")
        single = [base_request()["samples"][0]]
        self.assert_code(base_request(samples=single), "invalid_samples")
        self.assert_code(
            base_request(samples=[{"timestamp_s": 0}, "x"]), "invalid_samples"
        )

    def test_timestamp_rules(self) -> None:
        missing = [
            {"soc": 0.0},
            {"timestamp_s": 1, "soc": 1.0},
        ]
        self.assert_code(base_request(samples=missing), "invalid_timestamp")
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 0, "soc": 0.0},
                    {"timestamp_s": float("nan"), "soc": 1.0},
                ]
            ),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 1, "soc": 0.0},
                    {"timestamp_s": 1, "soc": 1.0},
                ]
            ),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": True, "soc": 0.0},
                    {"timestamp_s": 1, "soc": 1.0},
                ]
            ),
            "invalid_timestamp",
        )

    def test_soc_rules(self) -> None:
        missing = [
            {"timestamp_s": 0},
            {"timestamp_s": 1, "soc": 1.0},
        ]
        self.assert_code(base_request(samples=missing), "invalid_soc")
        for bad in (-0.01, 1.01, float("inf"), "x", True, None):
            self.assert_code(
                base_request(
                    samples=[
                        {"timestamp_s": 0, "soc": 0.0},
                        {"timestamp_s": 1, "soc": bad},
                    ]
                ),
                "invalid_soc",
            )

    def test_cycle_life_curve_rules(self) -> None:
        self.assert_code(
            {k: v for k, v in base_request().items() if k != "cycle_life_curve"},
            "invalid_cycle_life_curve",
        )
        self.assert_code(base_request(cycle_life_curve=[]), "invalid_cycle_life_curve")
        single = [life_curve()[0]]
        self.assert_code(
            base_request(cycle_life_curve=single), "invalid_cycle_life_curve"
        )
        self.assert_code(
            base_request(cycle_life_curve="x"), "invalid_cycle_life_curve"
        )
        self.assert_code(
            base_request(cycle_life_curve=[{}, life_curve()[1]]),
            "invalid_cycle_life_curve",
        )
        # depth out of (0, 1]
        self.assert_code(
            base_request(
                cycle_life_curve=[
                    {"depth": 0.0, "cycles_to_eol": 100.0},
                    {"depth": 1.0, "cycles_to_eol": 50.0},
                ]
            ),
            "invalid_cycle_life_curve",
        )
        # cycles_to_eol must be positive finite
        self.assert_code(
            base_request(
                cycle_life_curve=[
                    {"depth": 0.5, "cycles_to_eol": 0},
                    {"depth": 1.0, "cycles_to_eol": 50.0},
                ]
            ),
            "invalid_cycle_life_curve",
        )
        # depth must be strictly increasing
        self.assert_code(
            base_request(
                cycle_life_curve=[
                    {"depth": 0.5, "cycles_to_eol": 100.0},
                    {"depth": 0.5, "cycles_to_eol": 50.0},
                ]
            ),
            "invalid_cycle_life_curve",
        )
        # cycles_to_eol must be non-increasing
        self.assert_code(
            base_request(
                cycle_life_curve=[
                    {"depth": 0.5, "cycles_to_eol": 100.0},
                    {"depth": 1.0, "cycles_to_eol": 101.0},
                ]
            ),
            "invalid_cycle_life_curve",
        )

    def test_min_cycle_depth_rules(self) -> None:
        for bad in (-0.01, 1.01, "x", True, False, float("nan")):
            self.assert_code(base_request(min_cycle_depth=bad), "invalid_options")


class BatteryCyclesHttpTest(unittest.TestCase):
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

    def test_route_returns_200_and_matches_service(self) -> None:
        payload = base_request()
        status, body = self.post(
            BATTERY_CYCLES_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 200)
        service_result = Service().analyze_battery_cycles(payload)
        self.assertEqual(body, service_result)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_CYCLES_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_CYCLES_ROUTE, b"{bad")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_top_level_array_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_CYCLES_ROUTE, b"[1,2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_samples_is_422(self) -> None:
        payload = base_request(samples=[])
        status, body = self.post(
            BATTERY_CYCLES_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/cycles/nope", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
