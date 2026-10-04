import json
import math
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import Handler, OCV_CURVE_FIT_ROUTE
from powerrail.service import ApiError, Service


def base_request(**overrides):
    payload = {
        "measurements": [
            {"soc": 0.1, "voltage_v": 3.0},
            {"soc": 0.5, "voltage_v": 3.6},
            {"soc": 0.9, "voltage_v": 4.1},
        ],
    }
    payload.update(overrides)
    return payload


class OcvCurveFitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_exact_fit_passes_through_measurements(self) -> None:
        result = self.service.fit_ocv_curve(base_request())
        self.assertEqual(result["status"], "fitted")
        self.assertEqual(result["measurement_count"], 3)
        self.assertEqual(result["knot_count"], 3)
        self.assertAlmostEqual(result["rmse_voltage_v"], 0.0)
        self.assertAlmostEqual(result["max_abs_error_v"], 0.0)
        curve = result["curve"]
        self.assertEqual([point["soc"] for point in curve], [0.1, 0.5, 0.9])
        for point, voltage in zip(curve, (3.0, 3.6, 4.1)):
            self.assertAlmostEqual(point["voltage_v"], voltage)
            self.assertEqual(point["sample_count"], 1)
            self.assertAlmostEqual(point["weight_sum"], 1.0)
        for index, residual in enumerate(result["residuals"]):
            self.assertEqual(residual["index"], index)
            self.assertAlmostEqual(residual["residual_voltage_v"], 0.0)

    def test_duplicate_soc_merges_into_weighted_mean_knot(self) -> None:
        payload = base_request(
            measurements=[
                {"soc": 0.1, "voltage_v": 3.0},
                {"soc": 0.5, "voltage_v": 3.6, "weight": 1.0},
                {"soc": 0.5, "voltage_v": 3.8, "weight": 3.0},
                {"soc": 0.9, "voltage_v": 4.1},
            ]
        )
        result = self.service.fit_ocv_curve(payload)
        self.assertEqual(result["measurement_count"], 4)
        self.assertEqual(result["knot_count"], 3)
        middle = result["curve"][1]
        self.assertEqual(middle["soc"], 0.5)
        self.assertAlmostEqual(middle["voltage_v"], 3.75)
        self.assertEqual(middle["sample_count"], 2)
        self.assertAlmostEqual(middle["weight_sum"], 4.0)
        residuals = result["residuals"]
        self.assertAlmostEqual(residuals[1]["fitted_voltage_v"], 3.75)
        self.assertAlmostEqual(residuals[1]["residual_voltage_v"], -0.15)
        self.assertAlmostEqual(residuals[2]["fitted_voltage_v"], 3.75)
        self.assertAlmostEqual(residuals[2]["residual_voltage_v"], 0.05)
        expected_rmse = math.sqrt((1.0 * 0.15**2 + 3.0 * 0.05**2) / 6.0)
        self.assertAlmostEqual(result["rmse_voltage_v"], expected_rmse)
        self.assertAlmostEqual(result["max_abs_error_v"], 0.15)

    def test_decreasing_measurements_are_pooled_to_monotone_fit(self) -> None:
        payload = base_request(
            measurements=[
                {"soc": 0.2, "voltage_v": 3.8},
                {"soc": 0.4, "voltage_v": 3.6},
                {"soc": 0.6, "voltage_v": 3.7},
            ]
        )
        result = self.service.fit_ocv_curve(payload)
        voltages = [point["voltage_v"] for point in result["curve"]]
        self.assertAlmostEqual(voltages[0], 3.7 - 1e-6)
        self.assertAlmostEqual(voltages[1], 3.7)
        self.assertAlmostEqual(voltages[2], 3.7 + 1e-6)
        for left, right in zip(voltages, voltages[1:]):
            self.assertLess(left, right)

    def test_min_voltage_step_is_enforced(self) -> None:
        payload = base_request(
            measurements=[
                {"soc": 0.2, "voltage_v": 3.7},
                {"soc": 0.5, "voltage_v": 3.7},
            ],
            options={"min_voltage_step_v": 0.01},
        )
        result = self.service.fit_ocv_curve(payload)
        voltages = [point["voltage_v"] for point in result["curve"]]
        self.assertAlmostEqual(voltages[0], 3.695)
        self.assertAlmostEqual(voltages[1], 3.705)
        self.assertAlmostEqual(voltages[1] - voltages[0], 0.01)
        self.assertLess(voltages[0], voltages[1])
        self.assertAlmostEqual(result["rmse_voltage_v"], 0.005)
        self.assertAlmostEqual(result["max_abs_error_v"], 0.005)

    def test_residuals_follow_input_order_not_soc_order(self) -> None:
        payload = base_request(
            measurements=[
                {"soc": 0.9, "voltage_v": 4.1},
                {"soc": 0.1, "voltage_v": 3.0},
                {"soc": 0.5, "voltage_v": 3.6},
            ]
        )
        result = self.service.fit_ocv_curve(payload)
        residuals = result["residuals"]
        self.assertEqual([item["index"] for item in residuals], [0, 1, 2])
        self.assertEqual([item["soc"] for item in residuals], [0.9, 0.1, 0.5])
        self.assertEqual(
            [item["measured_voltage_v"] for item in residuals], [4.1, 3.0, 3.6]
        )
        self.assertAlmostEqual(residuals[0]["fitted_voltage_v"], 4.1)
        self.assertAlmostEqual(residuals[1]["fitted_voltage_v"], 3.0)
        self.assertAlmostEqual(residuals[2]["fitted_voltage_v"], 3.6)

    def test_curve_is_usable_by_existing_battery_entries(self) -> None:
        result = self.service.fit_ocv_curve(base_request())
        curve = [
            {"soc": point["soc"], "voltage_v": point["voltage_v"]}
            for point in result["curve"]
        ]
        soc_result = self.service.estimate_soc(
            {
                "capacity_ah": 1.0,
                "initial_soc": 0.5,
                "ocv_curve": curve,
                "samples": [
                    {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.6},
                    {"timestamp_s": 150, "current_a": 0.0, "voltage_v": 3.6},
                    {"timestamp_s": 300, "current_a": 0.0, "voltage_v": 3.6},
                ],
            }
        )
        self.assertEqual(soc_result["estimates"][-1]["source"], "ocv_corrected")
        model_result = self.service.simulate_battery_model(
            {
                "capacity_ah": 1.0,
                "initial_soc": 0.5,
                "model": {"r0_ohm": 0.1, "r1_ohm": 0.05, "c1_f": 1000.0},
                "ocv_curve": curve,
                "samples": [
                    {"timestamp_s": 0, "current_a": 0.5},
                    {"timestamp_s": 60, "current_a": 0.5},
                ],
            }
        )
        self.assertEqual(len(model_result["estimates"]), 2)

    def test_unknown_fields_are_ignored_and_request_is_not_modified(self) -> None:
        payload = base_request(
            options={"min_voltage_step_v": 0.001, "unknown_option": 1},
            extra_field="ignored",
        )
        payload["measurements"][0]["unknown_field"] = True
        snapshot = json.loads(json.dumps(payload))
        result = self.service.fit_ocv_curve(payload)
        self.assertEqual(result["status"], "fitted")
        self.assertEqual(payload, snapshot)


class OcvCurveFitValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.fit_ocv_curve(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_measurements_rules(self) -> None:
        self.assert_code(base_request(measurements=None), "invalid_measurements")
        self.assert_code(base_request(measurements="x"), "invalid_measurements")
        self.assert_code(base_request(measurements=[]), "invalid_measurements")
        self.assert_code(
            base_request(measurements=[{"soc": 0.1, "voltage_v": 3.0}]),
            "invalid_measurements",
        )
        self.assert_code(base_request(measurements=[1, 2]), "invalid_measurements")
        self.assert_code(
            {k: v for k, v in base_request().items() if k != "measurements"},
            "invalid_measurements",
        )

    def test_soc_rules(self) -> None:
        for bad in (None, -0.1, 1.1, float("nan"), float("inf"), True, "0.5"):
            items = [
                {"soc": 0.1, "voltage_v": 3.0},
                {"soc": bad, "voltage_v": 3.6},
            ]
            self.assert_code(base_request(measurements=items), "invalid_soc")
        missing = [{"soc": 0.1, "voltage_v": 3.0}, {"voltage_v": 3.6}]
        self.assert_code(base_request(measurements=missing), "invalid_soc")

    def test_voltage_rules(self) -> None:
        for bad in (None, float("nan"), float("inf"), False, "3.6"):
            items = [
                {"soc": 0.1, "voltage_v": 3.0},
                {"soc": 0.5, "voltage_v": bad},
            ]
            self.assert_code(base_request(measurements=items), "invalid_voltage")
        missing = [{"soc": 0.1, "voltage_v": 3.0}, {"soc": 0.5}]
        self.assert_code(base_request(measurements=missing), "invalid_voltage")

    def test_weight_rules(self) -> None:
        for bad in (0, -1.0, float("nan"), float("inf"), True, "1"):
            items = [
                {"soc": 0.1, "voltage_v": 3.0},
                {"soc": 0.5, "voltage_v": 3.6, "weight": bad},
            ]
            self.assert_code(base_request(measurements=items), "invalid_weight")

    def test_fit_options_rules(self) -> None:
        self.assert_code(base_request(options=[]), "invalid_fit_options")
        self.assert_code(base_request(options="x"), "invalid_fit_options")
        for bad in (0, -0.1, float("nan"), float("inf"), True, "0.001"):
            self.assert_code(
                base_request(options={"min_voltage_step_v": bad}),
                "invalid_fit_options",
            )

    def test_single_unique_soc_is_insufficient_span(self) -> None:
        payload = base_request(
            measurements=[
                {"soc": 0.5, "voltage_v": 3.6},
                {"soc": 0.5, "voltage_v": 3.7},
            ]
        )
        self.assert_code(payload, "insufficient_soc_span")


class OcvCurveFitHttpTest(unittest.TestCase):
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

    def post(self, route, raw):
        request = urllib.request.Request(
            self.base + route,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read())
            exc.close()
            return exc.code, body

    def test_fit_route_returns_200(self) -> None:
        status, body = self.post(OCV_CURVE_FIT_ROUTE, json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "fitted")
        self.assertEqual(body["knot_count"], 3)
        socs = [point["soc"] for point in body["curve"]]
        voltages = [point["voltage_v"] for point in body["curve"]]
        self.assertEqual(socs, sorted(socs))
        self.assertTrue(all(a < b for a, b in zip(voltages, voltages[1:])))

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(OCV_CURVE_FIT_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(OCV_CURVE_FIT_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(OCV_CURVE_FIT_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_measurements_is_422(self) -> None:
        status, body = self.post(OCV_CURVE_FIT_ROUTE, b"{}")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_measurements")

    def test_insufficient_span_is_422(self) -> None:
        payload = {
            "measurements": [
                {"soc": 0.5, "voltage_v": 3.6},
                {"soc": 0.5, "voltage_v": 3.7},
            ]
        }
        status, body = self.post(OCV_CURVE_FIT_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "insufficient_soc_span")

    def test_unknown_post_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/ocv/curve/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_healthz_get_still_works(self) -> None:
        with urllib.request.urlopen(self.base + "/healthz") as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
