import json
import math
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import (
    BATTERY_MODEL_ROUTE,
    HEALTH_ROUTE,
    OCV_CURVE_FIT_ROUTE,
    SOC_ROUTE,
    Handler,
)
from powerrail.service import ApiError, Service


def measurement(soc, voltage_v, **extra):
    values = {"soc": soc, "voltage_v": voltage_v}
    values.update(extra)
    return values


def base_request(**overrides):
    payload = {
        "measurements": [
            {"soc": 0.0, "voltage_v": 3.0},
            {"soc": 0.5, "voltage_v": 3.5},
            {"soc": 1.0, "voltage_v": 4.0},
        ]
    }
    payload.update(overrides)
    return payload


class FitOcvCurveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_monotone_measurements_fit_exactly(self) -> None:
        result = self.service.fit_ocv_curve(base_request())
        self.assertEqual(result["status"], "fitted")
        self.assertEqual(result["measurement_count"], 3)
        self.assertEqual(result["knot_count"], 3)
        self.assertEqual(
            [knot["soc"] for knot in result["curve"]], [0.0, 0.5, 1.0]
        )
        self.assertEqual(
            [knot["voltage_v"] for knot in result["curve"]], [3.0, 3.5, 4.0]
        )
        for knot in result["curve"]:
            self.assertEqual(knot["sample_count"], 1)
            self.assertEqual(knot["weight_sum"], 1.0)
        self.assertEqual(result["rmse_voltage_v"], 0.0)
        self.assertEqual(result["max_abs_error_v"], 0.0)
        for index, residual in enumerate(result["residuals"]):
            self.assertEqual(residual["index"], index)
            self.assertEqual(residual["residual_voltage_v"], 0.0)

    def test_curve_is_strictly_increasing_in_both_axes(self) -> None:
        result = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.9, "voltage_v": 4.0},
                    {"soc": 0.1, "voltage_v": 3.0},
                    {"soc": 0.5, "voltage_v": 3.5},
                ]
            }
        )
        socs = [knot["soc"] for knot in result["curve"]]
        voltages = [knot["voltage_v"] for knot in result["curve"]]
        self.assertEqual(socs, sorted(socs))
        self.assertTrue(
            all(left < right for left, right in zip(socs, socs[1:]))
        )
        self.assertTrue(
            all(left < right for left, right in zip(voltages, voltages[1:]))
        )

    def test_isotonic_pooling_minimizes_weighted_squares(self) -> None:
        result = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.0, "voltage_v": 3.0},
                    {"soc": 0.5, "voltage_v": 3.8},
                    {"soc": 1.0, "voltage_v": 3.6},
                ]
            }
        )
        voltages = [knot["voltage_v"] for knot in result["curve"]]
        self.assertEqual(voltages[0], 3.0)
        self.assertAlmostEqual(voltages[1], 3.7 - 0.0000005)
        self.assertAlmostEqual(voltages[2], 3.7 + 0.0000005)
        self.assertGreaterEqual(voltages[2] - voltages[1], 0.000001 - 1e-12)
        residuals = result["residuals"]
        self.assertAlmostEqual(
            residuals[1]["residual_voltage_v"], 3.8 - voltages[1]
        )
        self.assertAlmostEqual(
            residuals[2]["residual_voltage_v"], 3.6 - voltages[2]
        )
        self.assertAlmostEqual(
            result["max_abs_error_v"],
            max(abs(item["residual_voltage_v"]) for item in residuals),
        )

    def test_duplicate_socs_merge_into_one_weighted_knot(self) -> None:
        result = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.2, "voltage_v": 3.2, "weight": 2},
                    {"soc": 0.8, "voltage_v": 3.9},
                    {"soc": 0.2, "voltage_v": 3.4},
                ]
            }
        )
        self.assertEqual(result["knot_count"], 2)
        first, second = result["curve"]
        self.assertEqual(first["soc"], 0.2)
        self.assertAlmostEqual(first["voltage_v"], (2 * 3.2 + 3.4) / 3)
        self.assertEqual(first["sample_count"], 2)
        self.assertEqual(first["weight_sum"], 3.0)
        self.assertEqual(second["sample_count"], 1)
        self.assertEqual(second["weight_sum"], 1.0)
        # residuals stay in input order
        self.assertEqual(
            [item["index"] for item in result["residuals"]], [0, 1, 2]
        )
        self.assertEqual(
            [item["soc"] for item in result["residuals"]], [0.2, 0.8, 0.2]
        )
        self.assertEqual(
            result["residuals"][0]["fitted_voltage_v"], first["voltage_v"]
        )
        self.assertEqual(
            result["residuals"][2]["fitted_voltage_v"], first["voltage_v"]
        )
        weighted_sse = (
            2 * (3.2 - first["voltage_v"]) ** 2
            + (3.4 - first["voltage_v"]) ** 2
        )
        self.assertAlmostEqual(
            result["rmse_voltage_v"], math.sqrt(weighted_sse / 4.0)
        )

    def test_residual_sign_is_measured_minus_fitted(self) -> None:
        result = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.0, "voltage_v": 3.0},
                    {"soc": 1.0, "voltage_v": 4.2},
                ]
            }
        )
        for item in result["residuals"]:
            self.assertAlmostEqual(
                item["residual_voltage_v"],
                item["measured_voltage_v"] - item["fitted_voltage_v"],
            )

    def test_fitted_value_at_each_input_equals_its_knot_value(self) -> None:
        result = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.0, "voltage_v": 3.0},
                    {"soc": 0.25, "voltage_v": 3.1},
                    {"soc": 0.75, "voltage_v": 4.1},
                    {"soc": 1.0, "voltage_v": 4.0},
                ]
            }
        )
        knot_by_soc = {knot["soc"]: knot["voltage_v"] for knot in result["curve"]}
        for item in result["residuals"]:
            self.assertEqual(
                item["fitted_voltage_v"], knot_by_soc[item["soc"]]
            )
        voltages = [knot["voltage_v"] for knot in result["curve"]]
        for left, right in zip(voltages, voltages[1:]):
            self.assertGreaterEqual(right - left, 0.000001 - 1e-12)

    def test_min_voltage_step_forces_spacing(self) -> None:
        result = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.0, "voltage_v": 3.0},
                    {"soc": 0.5, "voltage_v": 3.0},
                    {"soc": 1.0, "voltage_v": 3.0},
                ],
                "options": {"min_voltage_step_v": 0.01},
            }
        )
        voltages = [knot["voltage_v"] for knot in result["curve"]]
        for left, right in zip(voltages, voltages[1:]):
            self.assertGreaterEqual(right - left, 0.01 - 1e-12)

    def test_weighted_fit_leans_toward_heavier_samples(self) -> None:
        heavy = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.0, "voltage_v": 3.0},
                    {"soc": 0.5, "voltage_v": 3.9, "weight": 10},
                    {"soc": 1.0, "voltage_v": 3.6, "weight": 1},
                ]
            }
        )
        light = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.0, "voltage_v": 3.0},
                    {"soc": 0.5, "voltage_v": 3.9, "weight": 1},
                    {"soc": 1.0, "voltage_v": 3.6, "weight": 10},
                ]
            }
        )
        self.assertGreater(
            heavy["curve"][1]["voltage_v"], light["curve"][1]["voltage_v"]
        )

    def test_unknown_fields_ignored_and_request_not_modified(self) -> None:
        payload = {
            "measurements": [
                {"soc": 0.0, "voltage_v": 3.0, "extra": "x"},
                {"soc": 1.0, "voltage_v": 4.0},
            ],
            "options": {"min_voltage_step_v": 0.001, "unknown": 7},
            "junk": True,
        }
        snapshot = json.loads(json.dumps(payload))
        result = self.service.fit_ocv_curve(payload)
        self.assertEqual(payload, snapshot)
        self.assertEqual(result["knot_count"], 2)

    def test_default_weight_is_one(self) -> None:
        result = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0.0, "voltage_v": 3.0},
                    {"soc": 1.0, "voltage_v": 4.0, "weight": 1},
                ]
            }
        )
        for knot in result["curve"]:
            self.assertEqual(knot["weight_sum"], 1.0)

    def test_fitted_curve_feeds_battery_endpoints(self) -> None:
        fitted = self.service.fit_ocv_curve(base_request())
        curve = [
            {"soc": knot["soc"], "voltage_v": knot["voltage_v"]}
            for knot in fitted["curve"]
        ]
        model_result = self.service.simulate_battery_model(
            {
                "capacity_ah": 1.0,
                "initial_soc": 0.5,
                "model": {"r0_ohm": 0.1, "r1_ohm": 0.2, "c1_f": 100.0},
                "ocv_curve": curve,
                "samples": [{"timestamp_s": 0, "current_a": 0.0}],
            }
        )
        self.assertAlmostEqual(model_result["estimates"][0]["ocv_voltage_v"], 3.5)
        soc_result = self.service.estimate_soc(
            {
                "capacity_ah": 1.0,
                "initial_soc": 0.5,
                "samples": [
                    {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.5},
                    {"timestamp_s": 600, "current_a": 0.0, "voltage_v": 4.0},
                ],
                "ocv_curve": [
                    {"soc": knot["soc"], "voltage_v": knot["voltage_v"]}
                    for knot in fitted["curve"]
                ],
            }
        )
        self.assertEqual(soc_result["estimates"][-1]["source"], "ocv_corrected")
        self.assertAlmostEqual(soc_result["final_soc"], 0.8 * soc_result["estimates"][0]["soc"] + 0.2 * 1.0)


class FitOcvCurveValidationTest(unittest.TestCase):
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
        self.assert_code({}, "invalid_measurements")
        self.assert_code({"measurements": None}, "invalid_measurements")
        self.assert_code({"measurements": "x"}, "invalid_measurements")
        self.assert_code({"measurements": []}, "invalid_measurements")
        self.assert_code(
            {"measurements": [{"soc": 0.0, "voltage_v": 3.0}]},
            "invalid_measurements",
        )
        self.assert_code(
            {"measurements": [{"soc": 0.0, "voltage_v": 3.0}, 4]},
            "invalid_measurements",
        )

    def test_soc_rules(self) -> None:
        base = [
            {"soc": 0.0, "voltage_v": 3.0},
            {"soc": 1.0, "voltage_v": 4.0},
        ]
        for bad in (-0.1, 1.1, True, "0.5", None, float("nan"), float("inf")):
            self.assert_code(
                {"measurements": [base[0], {"soc": bad, "voltage_v": 4.0}]},
                "invalid_soc",
            )

    def test_voltage_rules(self) -> None:
        for bad in (None, "3.5", True, float("nan"), float("inf")):
            self.assert_code(
                {
                    "measurements": [
                        {"soc": 0.0, "voltage_v": 3.0},
                        {"soc": 1.0, "voltage_v": bad},
                    ]
                },
                "invalid_voltage",
            )

    def test_weight_rules(self) -> None:
        for bad in (0, -1.0, True, "1", float("nan"), float("inf")):
            self.assert_code(
                {
                    "measurements": [
                        {"soc": 0.0, "voltage_v": 3.0},
                        {"soc": 1.0, "voltage_v": 4.0, "weight": bad},
                    ]
                },
                "invalid_weight",
            )

    def test_options_rules(self) -> None:
        good = [
            {"soc": 0.0, "voltage_v": 3.0},
            {"soc": 1.0, "voltage_v": 4.0},
        ]
        self.assert_code({"measurements": good, "options": []}, "invalid_fit_options")
        self.assert_code({"measurements": good, "options": 5}, "invalid_fit_options")
        for bad in (0, -0.001, True, "1e-6", float("nan"), float("inf")):
            self.assert_code(
                {
                    "measurements": good,
                    "options": {"min_voltage_step_v": bad},
                },
                "invalid_fit_options",
            )

    def test_single_unique_soc_is_insufficient_span(self) -> None:
        self.assert_code(
            {
                "measurements": [
                    {"soc": 0.5, "voltage_v": 3.4},
                    {"soc": 0.5, "voltage_v": 3.5},
                    {"soc": 0.5, "voltage_v": 3.6, "weight": 2},
                ]
            },
            "insufficient_soc_span",
        )

    def test_endpoint_socs_are_accepted(self) -> None:
        result = self.service.fit_ocv_curve(
            {
                "measurements": [
                    {"soc": 0, "voltage_v": 3.0},
                    {"soc": 1, "voltage_v": 4.0},
                ]
            }
        )
        self.assertEqual(result["knot_count"], 2)


class FitOcvCurveHttpTest(unittest.TestCase):
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
        status, body = self.post(
            OCV_CURVE_FIT_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "fitted")
        self.assertEqual(body["knot_count"], 3)
        self.assertEqual(body["measurement_count"], 3)
        self.assertEqual(len(body["curve"]), 3)
        self.assertEqual(len(body["residuals"]), 3)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(OCV_CURVE_FIT_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(OCV_CURVE_FIT_ROUTE, b"{bad")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(OCV_CURVE_FIT_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_validation_errors_are_422(self) -> None:
        status, body = self.post(
            OCV_CURVE_FIT_ROUTE,
            json.dumps({"measurements": [{"soc": 0.5}]}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_measurements")

        status, body = self.post(
            OCV_CURVE_FIT_ROUTE,
            json.dumps(
                {
                    "measurements": [
                        {"soc": 2.0, "voltage_v": 3.0},
                        {"soc": 1.0, "voltage_v": 4.0},
                    ]
                }
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_soc")

        status, body = self.post(
            OCV_CURVE_FIT_ROUTE,
            json.dumps(
                {
                    "measurements": [
                        {"soc": 0.0, "voltage_v": True},
                        {"soc": 1.0, "voltage_v": 4.0},
                    ]
                }
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_voltage")

        status, body = self.post(
            OCV_CURVE_FIT_ROUTE,
            json.dumps(
                {
                    "measurements": [
                        {"soc": 0.0, "voltage_v": 3.0},
                        {"soc": 1.0, "voltage_v": 4.0, "weight": -1},
                    ]
                }
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_weight")

        status, body = self.post(
            OCV_CURVE_FIT_ROUTE,
            json.dumps(
                {
                    "measurements": [
                        {"soc": 0.5, "voltage_v": 3.0},
                        {"soc": 0.5, "voltage_v": 4.0},
                    ]
                }
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "insufficient_soc_span")

        status, body = self.post(
            OCV_CURVE_FIT_ROUTE,
            json.dumps(
                {
                    "measurements": [
                        {"soc": 0.0, "voltage_v": 3.0},
                        {"soc": 1.0, "voltage_v": 4.0},
                    ],
                    "options": {"min_voltage_step_v": 0},
                }
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_fit_options")

    def test_unknown_path_and_existing_routes_unchanged(self) -> None:
        status, body = self.post("/v1/battery/ocv/curve/unknown", b"")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

        status, body = self.post(SOC_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        status, body = self.post(BATTERY_MODEL_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        status, body = self.post(HEALTH_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        with urllib.request.urlopen(self.base + "/healthz") as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
