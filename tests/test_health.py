import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import HEALTH_ROUTE, SOC_ROUTE, Handler
from powerrail.service import ApiError, Service


def base_request(**overrides):
    payload = {
        "nominal_capacity_ah": 100.0,
        "measurements": [
            {"timestamp_s": 0, "measured_capacity_ah": 100.0, "cumulative_discharge_ah": 0.0},
            {
                "timestamp_s": 100,
                "measured_capacity_ah": 90.0,
                "cumulative_discharge_ah": 100.0,
            },
        ],
    }
    payload.update(overrides)
    return payload


class HealthEstimationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_per_item_estimates_match_measurements_in_order(self) -> None:
        result = self.service.estimate_health(base_request())
        estimates = result["estimates"]
        self.assertEqual(
            [item["timestamp_s"] for item in estimates], [0, 100]
        )
        self.assertAlmostEqual(estimates[0]["soh"], 1.0)
        self.assertAlmostEqual(estimates[0]["equivalent_cycles"], 0.0)
        self.assertAlmostEqual(estimates[1]["soh"], 0.9)
        self.assertAlmostEqual(estimates[1]["equivalent_cycles"], 1.0)
        self.assertAlmostEqual(result["latest_soh"], 0.9)
        self.assertAlmostEqual(result["consumed_cycles"], 1.0)

    def test_soh_is_clamped_to_unit_range(self) -> None:
        payload = base_request(
            measurements=[
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 120.0,
                    "cumulative_discharge_ah": 0.0,
                },
                {
                    "timestamp_s": 10,
                    "measured_capacity_ah": 0.5,
                    "cumulative_discharge_ah": 50.0,
                },
            ]
        )
        estimates = self.service.estimate_health(payload)["estimates"]
        self.assertEqual(estimates[0]["soh"], 1.0)
        self.assertAlmostEqual(estimates[1]["soh"], 0.005)

    def test_projection_extrapolates_degradation_linearly(self) -> None:
        # soh drops 0.1 over one equivalent cycle -> 0.1 per cycle;
        # 0.9 down to the 0.8 threshold leaves one cycle.
        result = self.service.estimate_health(base_request())
        self.assertEqual(result["projection_status"], "projected")
        self.assertAlmostEqual(result["remaining_cycles"], 1.0)

    def test_projection_respects_custom_threshold(self) -> None:
        result = self.service.estimate_health(base_request(end_of_life_soh=0.85))
        self.assertEqual(result["projection_status"], "projected")
        self.assertAlmostEqual(result["remaining_cycles"], 0.5)

    def test_latest_soh_at_threshold_is_end_of_life(self) -> None:
        payload = base_request(
            measurements=[
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 100.0,
                    "cumulative_discharge_ah": 0.0,
                },
                {
                    "timestamp_s": 100,
                    "measured_capacity_ah": 80.0,
                    "cumulative_discharge_ah": 200.0,
                },
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "end_of_life")
        self.assertEqual(result["remaining_cycles"], 0)

    def test_latest_soh_below_threshold_is_end_of_life(self) -> None:
        payload = base_request(
            measurements=[
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 100.0,
                    "cumulative_discharge_ah": 0.0,
                },
                {
                    "timestamp_s": 100,
                    "measured_capacity_ah": 70.0,
                    "cumulative_discharge_ah": 300.0,
                },
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "end_of_life")
        self.assertEqual(result["remaining_cycles"], 0)

    def test_no_soh_drop_is_insufficient_trend(self) -> None:
        payload = base_request(
            measurements=[
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 95.0,
                    "cumulative_discharge_ah": 0.0,
                },
                {
                    "timestamp_s": 100,
                    "measured_capacity_ah": 95.0,
                    "cumulative_discharge_ah": 100.0,
                },
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "insufficient_trend")
        self.assertIsNone(result["remaining_cycles"])

    def test_zero_cycle_span_is_insufficient_trend(self) -> None:
        payload = base_request(
            measurements=[
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 100.0,
                    "cumulative_discharge_ah": 50.0,
                },
                {
                    "timestamp_s": 100,
                    "measured_capacity_ah": 90.0,
                    "cumulative_discharge_ah": 50.0,
                },
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "insufficient_trend")
        self.assertIsNone(result["remaining_cycles"])

    def test_soh_growth_is_insufficient_trend(self) -> None:
        payload = base_request(
            measurements=[
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 90.0,
                    "cumulative_discharge_ah": 0.0,
                },
                {
                    "timestamp_s": 100,
                    "measured_capacity_ah": 95.0,
                    "cumulative_discharge_ah": 100.0,
                },
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "insufficient_trend")
        self.assertIsNone(result["remaining_cycles"])

    def test_threshold_of_zero_requires_zero_soh(self) -> None:
        payload = base_request(
            end_of_life_soh=0.0,
            measurements=[
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
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "projected")
        self.assertAlmostEqual(result["remaining_cycles"], 9.0)

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.estimate_health(payload)
        self.assertEqual(payload, snapshot)


class HealthValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_health(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_nominal_capacity_rules(self) -> None:
        self.assert_code(base_request(nominal_capacity_ah=None), "invalid_nominal_capacity")
        self.assert_code(base_request(nominal_capacity_ah=0), "invalid_nominal_capacity")
        self.assert_code(base_request(nominal_capacity_ah=-1.0), "invalid_nominal_capacity")
        self.assert_code(base_request(nominal_capacity_ah=True), "invalid_nominal_capacity")
        self.assert_code(base_request(nominal_capacity_ah="x"), "invalid_nominal_capacity")
        self.assert_code(
            base_request(nominal_capacity_ah=float("nan")),
            "invalid_nominal_capacity",
        )
        self.assert_code(
            {k: v for k, v in base_request().items() if k != "nominal_capacity_ah"},
            "invalid_nominal_capacity",
        )

    def test_measurements_rules(self) -> None:
        self.assert_code(base_request(measurements=[]), "invalid_measurements")
        self.assert_code(
            base_request(measurements=[base_request()["measurements"][0]]),
            "invalid_measurements",
        )
        self.assert_code(base_request(measurements="x"), "invalid_measurements")
        self.assert_code(base_request(measurements=None), "invalid_measurements")
        self.assert_code(base_request(measurements=[1, 2]), "invalid_measurements")

    def test_end_of_life_soh_rules(self) -> None:
        self.assert_code(base_request(end_of_life_soh=-0.01), "invalid_end_of_life_soh")
        self.assert_code(base_request(end_of_life_soh=1.01), "invalid_end_of_life_soh")
        self.assert_code(base_request(end_of_life_soh="x"), "invalid_end_of_life_soh")
        self.assert_code(
            base_request(end_of_life_soh=float("inf")), "invalid_end_of_life_soh"
        )

    def test_timestamp_rules(self) -> None:
        missing = [
            {"measured_capacity_ah": 100.0, "cumulative_discharge_ah": 0.0},
            {"measured_capacity_ah": 90.0, "cumulative_discharge_ah": 100.0},
        ]
        self.assert_code(base_request(measurements=missing), "invalid_timestamp")
        non_finite = [
            {
                "timestamp_s": 0,
                "measured_capacity_ah": 100.0,
                "cumulative_discharge_ah": 0.0,
            },
            {
                "timestamp_s": float("inf"),
                "measured_capacity_ah": 90.0,
                "cumulative_discharge_ah": 100.0,
            },
        ]
        self.assert_code(base_request(measurements=non_finite), "invalid_timestamp")
        not_increasing = [
            {
                "timestamp_s": 100,
                "measured_capacity_ah": 100.0,
                "cumulative_discharge_ah": 0.0,
            },
            {
                "timestamp_s": 100,
                "measured_capacity_ah": 90.0,
                "cumulative_discharge_ah": 100.0,
            },
        ]
        self.assert_code(base_request(measurements=not_increasing), "invalid_timestamp")

    def test_capacity_measurement_rules(self) -> None:
        missing = [
            {"timestamp_s": 0, "cumulative_discharge_ah": 0.0},
            {"timestamp_s": 100, "cumulative_discharge_ah": 100.0},
        ]
        self.assert_code(
            base_request(measurements=missing), "invalid_capacity_measurement"
        )
        for bad in (0, -1.0, float("nan"), "x"):
            items = [
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 100.0,
                    "cumulative_discharge_ah": 0.0,
                },
                {
                    "timestamp_s": 100,
                    "measured_capacity_ah": bad,
                    "cumulative_discharge_ah": 100.0,
                },
            ]
            self.assert_code(
                base_request(measurements=items), "invalid_capacity_measurement"
            )

    def test_throughput_rules(self) -> None:
        missing = [
            {"timestamp_s": 0, "measured_capacity_ah": 100.0},
            {"timestamp_s": 100, "measured_capacity_ah": 90.0},
        ]
        self.assert_code(base_request(measurements=missing), "invalid_throughput")
        for bad in (-0.01, float("nan"), "x"):
            items = [
                {
                    "timestamp_s": 0,
                    "measured_capacity_ah": 100.0,
                    "cumulative_discharge_ah": 0.0,
                },
                {
                    "timestamp_s": 100,
                    "measured_capacity_ah": 90.0,
                    "cumulative_discharge_ah": bad,
                },
            ]
            self.assert_code(base_request(measurements=items), "invalid_throughput")
        decreasing = [
            {
                "timestamp_s": 0,
                "measured_capacity_ah": 100.0,
                "cumulative_discharge_ah": 100.0,
            },
            {
                "timestamp_s": 100,
                "measured_capacity_ah": 90.0,
                "cumulative_discharge_ah": 99.0,
            },
        ]
        self.assert_code(base_request(measurements=decreasing), "invalid_throughput")
        # Equal consecutive throughput is allowed: non-decreasing, not strictly.
        equal = [
            {
                "timestamp_s": 0,
                "measured_capacity_ah": 100.0,
                "cumulative_discharge_ah": 50.0,
            },
            {
                "timestamp_s": 100,
                "measured_capacity_ah": 90.0,
                "cumulative_discharge_ah": 50.0,
            },
        ]
        result = self.service.estimate_health(base_request(measurements=equal))
        self.assertEqual(result["projection_status"], "insufficient_trend")


class HealthHttpTest(unittest.TestCase):
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

    def test_health_estimate_route_returns_200(self) -> None:
        status, body = self.post(HEALTH_ROUTE, json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["projection_status"], "projected")
        self.assertAlmostEqual(body["remaining_cycles"], 1.0)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(HEALTH_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(HEALTH_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(HEALTH_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_nominal_capacity_is_422(self) -> None:
        payload = base_request(nominal_capacity_ah=-1)
        status, body = self.post(HEALTH_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_nominal_capacity")
        self.assertTrue(body["error"]["message"])

    def test_unknown_post_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_soc_route_still_works(self) -> None:
        payload = {
            "capacity_ah": 1.0,
            "initial_soc": 0.8,
            "samples": [
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 3.8},
                {"timestamp_s": 1800, "current_a": 1.0, "voltage_v": 3.7},
            ],
        }
        status, body = self.post(SOC_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["final_soc"], 0.3)

    def test_healthz_get_still_works(self) -> None:
        with urllib.request.urlopen(self.base + "/healthz") as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
