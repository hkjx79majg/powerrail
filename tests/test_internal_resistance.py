import json
import math
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import BATTERY_RESISTANCE_ROUTE, Handler
from powerrail.service import ApiError, Service


def pulse(soc, current_before, current_after, voltage_before, voltage_after, **extra):
    item = {
        "soc": soc,
        "current_before_a": current_before,
        "current_after_a": current_after,
        "voltage_before_v": voltage_before,
        "voltage_after_v": voltage_after,
    }
    item.update(extra)
    return item


def base_request(**overrides):
    payload = {
        "pulses": [
            pulse(0.2, 0.0, 1.0, 3.8, 3.7),       # R = 0.10
            pulse(0.5, 1.0, 3.0, 3.6, 3.36),      # R = 0.12
            pulse(0.8, 2.0, 3.0, 3.4, 3.25),      # R = 0.15
        ],
    }
    payload.update(overrides)
    return payload


class InternalResistanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_exact_fit_passes_through_pulses(self) -> None:
        result = self.service.estimate_internal_resistance(base_request())
        self.assertEqual(result["status"], "fitted")
        self.assertAlmostEqual(result["recommended_r0_ohm"], 0.15)
        self.assertAlmostEqual(result["rmse_voltage_v"], 0.0)
        curve = result["curve"]
        self.assertEqual([point["soc"] for point in curve], [0.2, 0.5, 0.8])
        for point, resistance in zip(curve, (0.10, 0.12, 0.15)):
            self.assertAlmostEqual(point["resistance_ohm"], resistance)
            self.assertEqual(point["pulse_count"], 1)
            self.assertAlmostEqual(point["weight_sum"], 1.0)
        estimates = result["pulse_estimates"]
        self.assertEqual([item["index"] for item in estimates], [0, 1, 2])
        self.assertEqual([item["soc"] for item in estimates], [0.2, 0.5, 0.8])
        for item, delta_current, delta_voltage, resistance in zip(
            estimates,
            (1.0, 2.0, 1.0),
            (-0.1, -0.24, -0.15),
            (0.10, 0.12, 0.15),
        ):
            self.assertAlmostEqual(item["delta_current_a"], delta_current)
            self.assertAlmostEqual(item["delta_voltage_v"], delta_voltage)
            self.assertAlmostEqual(item["resistance_ohm"], resistance)
            self.assertAlmostEqual(
                item["fitted_voltage_change_v"], delta_voltage
            )
            self.assertAlmostEqual(item["residual_voltage_v"], 0.0)

    def test_charge_pulse_with_negative_current_step(self) -> None:
        payload = base_request(
            pulses=[
                pulse(0.2, 1.0, 0.0, 3.7, 3.8),
                pulse(0.8, 0.0, -1.0, 3.4, 3.55),
            ]
        )
        result = self.service.estimate_internal_resistance(payload)
        self.assertEqual(result["status"], "fitted")
        estimates = result["pulse_estimates"]
        self.assertAlmostEqual(estimates[0]["resistance_ohm"], 0.10)
        self.assertAlmostEqual(estimates[1]["resistance_ohm"], 0.15)
        self.assertAlmostEqual(estimates[0]["delta_current_a"], -1.0)
        self.assertAlmostEqual(estimates[0]["delta_voltage_v"], 0.10)
        self.assertAlmostEqual(result["recommended_r0_ohm"], 0.15)

    def test_duplicate_soc_merges_into_weighted_mean_knot(self) -> None:
        payload = base_request(
            pulses=[
                pulse(0.2, 0.0, 1.0, 3.8, 3.7),
                pulse(0.5, 0.0, 1.0, 3.6, 3.50),       # R = 0.10, w = 1
                pulse(0.5, 0.0, 1.0, 3.6, 3.46, weight=3.0),  # R = 0.14, w = 3
            ]
        )
        result = self.service.estimate_internal_resistance(payload)
        self.assertEqual(len(result["curve"]), 2)
        middle = result["curve"][1]
        self.assertEqual(middle["soc"], 0.5)
        self.assertAlmostEqual(middle["resistance_ohm"], 0.13)
        self.assertEqual(middle["pulse_count"], 2)
        self.assertAlmostEqual(middle["weight_sum"], 4.0)
        estimates = result["pulse_estimates"]
        self.assertAlmostEqual(estimates[1]["fitted_voltage_change_v"], -0.13)
        self.assertAlmostEqual(estimates[1]["residual_voltage_v"], 0.03)
        self.assertAlmostEqual(estimates[2]["fitted_voltage_change_v"], -0.13)
        self.assertAlmostEqual(estimates[2]["residual_voltage_v"], -0.01)
        expected_rmse = math.sqrt((1.0 * 0.03**2 + 3.0 * 0.01**2) / 5.0)
        self.assertAlmostEqual(result["rmse_voltage_v"], expected_rmse)

    def test_recommended_r0_is_the_largest_knot_resistance(self) -> None:
        payload = base_request(
            pulses=[
                pulse(0.2, 0.0, 2.0, 3.8, 3.5),   # 0.15
                pulse(0.8, 0.0, 2.0, 3.4, 3.2),   # 0.10
            ]
        )
        result = self.service.estimate_internal_resistance(payload)
        self.assertEqual([point["soc"] for point in result["curve"]], [0.2, 0.8])
        self.assertAlmostEqual(result["recommended_r0_ohm"], 0.15)

    def test_recommended_r0_is_usable_by_the_battery_model_entry(self) -> None:
        result = self.service.estimate_internal_resistance(base_request())
        model_result = self.service.simulate_battery_model(
            {
                "capacity_ah": 1.0,
                "initial_soc": 0.5,
                "model": {
                    "r0_ohm": result["recommended_r0_ohm"],
                    "r1_ohm": 0.05,
                    "c1_f": 1000.0,
                },
                "ocv_curve": [
                    {"soc": 0.0, "voltage_v": 3.0},
                    {"soc": 1.0, "voltage_v": 4.2},
                ],
                "samples": [
                    {"timestamp_s": 0, "current_a": 0.5},
                    {"timestamp_s": 60, "current_a": 0.5},
                ],
            }
        )
        self.assertEqual(len(model_result["estimates"]), 2)

    def test_current_step_at_threshold_is_accepted(self) -> None:
        payload = base_request(
            min_current_step_a=0.5,
            pulses=[
                pulse(0.2, 0.0, 0.5, 3.8, 3.75),
                pulse(0.8, 0.0, 2.0, 3.4, 3.2),
            ],
        )
        result = self.service.estimate_internal_resistance(payload)
        self.assertEqual(result["status"], "fitted")

    def test_unknown_fields_are_ignored_and_request_is_not_modified(self) -> None:
        payload = base_request(
            min_current_step_a=0.05,
            extra_field="ignored",
        )
        payload["pulses"][0]["unknown_field"] = True
        snapshot = json.loads(json.dumps(payload))
        result = self.service.estimate_internal_resistance(payload)
        self.assertEqual(result["status"], "fitted")
        self.assertEqual(payload, snapshot)


class InternalResistanceValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_internal_resistance(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_pulses_rules(self) -> None:
        self.assert_code({}, "invalid_pulses")
        self.assert_code(base_request(pulses=None), "invalid_pulses")
        self.assert_code(base_request(pulses="x"), "invalid_pulses")
        self.assert_code(base_request(pulses=[]), "invalid_pulses")
        self.assert_code(base_request(pulses=[1, 2]), "invalid_pulses")

    def test_single_pulse_is_insufficient_span(self) -> None:
        self.assert_code(
            base_request(pulses=[pulse(0.5, 0.0, 1.0, 3.6, 3.5)]),
            "insufficient_soc_span",
        )

    def test_soc_rules(self) -> None:
        for bad in (None, -0.1, 1.1, float("nan"), float("inf"), True, "0.5"):
            self.assert_code(
                base_request(
                    pulses=[
                        pulse(bad, 0.0, 1.0, 3.8, 3.7),
                        pulse(0.8, 0.0, 1.0, 3.4, 3.25),
                    ]
                ),
                "invalid_soc",
            )
        missing = [
            {"current_before_a": 0.0, "current_after_a": 1.0,
             "voltage_before_v": 3.8, "voltage_after_v": 3.7},
            pulse(0.8, 0.0, 1.0, 3.4, 3.25),
        ]
        self.assert_code(base_request(pulses=missing), "invalid_soc")

    def test_current_rules(self) -> None:
        for bad in (None, float("nan"), float("inf"), True, "1"):
            self.assert_code(
                base_request(
                    pulses=[
                        pulse(0.2, bad, 1.0, 3.8, 3.7),
                        pulse(0.8, 0.0, 1.0, 3.4, 3.25),
                    ]
                ),
                "invalid_current",
            )
            self.assert_code(
                base_request(
                    pulses=[
                        pulse(0.2, 0.0, bad, 3.8, 3.7),
                        pulse(0.8, 0.0, 1.0, 3.4, 3.25),
                    ]
                ),
                "invalid_current",
            )

    def test_voltage_rules(self) -> None:
        for bad in (None, 0.0, -1.0, float("nan"), float("inf"), False, "3.7"):
            self.assert_code(
                base_request(
                    pulses=[
                        pulse(0.2, 0.0, 1.0, bad, 3.7),
                        pulse(0.8, 0.0, 1.0, 3.4, 3.25),
                    ]
                ),
                "invalid_voltage",
            )
            self.assert_code(
                base_request(
                    pulses=[
                        pulse(0.2, 0.0, 1.0, 3.8, bad),
                        pulse(0.8, 0.0, 1.0, 3.4, 3.25),
                    ]
                ),
                "invalid_voltage",
            )

    def test_weight_rules(self) -> None:
        for bad in (0, -1.0, float("nan"), float("inf"), True, "1"):
            self.assert_code(
                base_request(
                    pulses=[
                        pulse(0.2, 0.0, 1.0, 3.8, 3.7, weight=bad),
                        pulse(0.8, 0.0, 1.0, 3.4, 3.25),
                    ]
                ),
                "invalid_weight",
            )

    def test_options_rules(self) -> None:
        for bad in (0, -0.1, float("nan"), float("inf"), True, "0.1"):
            self.assert_code(
                base_request(min_current_step_a=bad),
                "invalid_options",
            )

    def test_current_step_below_threshold_is_rejected(self) -> None:
        payload = base_request(
            pulses=[
                pulse(0.2, 0.0, 0.05, 3.8, 3.79),
                pulse(0.8, 0.0, 1.0, 3.4, 3.25),
            ]
        )
        self.assert_code(payload, "invalid_current_step")
        self.assert_code(
            base_request(min_current_step_a=2.0), "invalid_current_step"
        )

    def test_same_direction_response_is_rejected(self) -> None:
        # Current rises and voltage also rises.
        payload = base_request(
            pulses=[
                pulse(0.2, 0.0, 1.0, 3.7, 3.8),
                pulse(0.8, 0.0, 1.0, 3.4, 3.25),
            ]
        )
        self.assert_code(payload, "invalid_pulse_response")
        # Current and voltage both unchanged: the step check fires first.
        flat = base_request(
            pulses=[
                pulse(0.2, 1.0, 1.0, 3.8, 3.8),
                pulse(0.8, 0.0, 1.0, 3.4, 3.25),
            ]
        )
        self.assert_code(flat, "invalid_current_step")

    def test_step_above_threshold_but_zero_voltage_change_is_bad_response(self) -> None:
        payload = base_request(
            pulses=[
                pulse(0.2, 0.0, 1.0, 3.8, 3.8),
                pulse(0.8, 0.0, 1.0, 3.4, 3.25),
            ]
        )
        self.assert_code(payload, "invalid_pulse_response")


class InternalResistanceHttpTest(unittest.TestCase):
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

    def test_estimate_route_returns_200(self) -> None:
        status, body = self.post(
            BATTERY_RESISTANCE_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "fitted")
        socs = [point["soc"] for point in body["curve"]]
        self.assertEqual(socs, sorted(socs))
        self.assertAlmostEqual(body["recommended_r0_ohm"], 0.15)
        self.assertEqual(len(body["pulse_estimates"]), 3)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_RESISTANCE_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_RESISTANCE_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_RESISTANCE_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_pulses_is_422(self) -> None:
        status, body = self.post(BATTERY_RESISTANCE_ROUTE, b"{}")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_pulses")

    def test_insufficient_span_is_422(self) -> None:
        payload = {"pulses": [pulse(0.5, 0.0, 1.0, 3.6, 3.5)]}
        status, body = self.post(
            BATTERY_RESISTANCE_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "insufficient_soc_span")

    def test_unknown_post_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/resistance/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_healthz_get_still_works(self) -> None:
        with urllib.request.urlopen(self.base + "/healthz") as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
