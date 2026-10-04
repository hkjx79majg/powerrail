import json
import math
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import Handler, RESISTANCE_ESTIMATE_ROUTE
from powerrail.service import ApiError, Service


def pulse(soc, current_before, current_after, voltage_before, voltage_after, **extra):
    values = {
        "soc": soc,
        "current_before_a": current_before,
        "current_after_a": current_after,
        "voltage_before_v": voltage_before,
        "voltage_after_v": voltage_after,
    }
    values.update(extra)
    return values


def discharge(soc, current_step, resistance, voltage_before=4.0, **extra):
    # Discharge: current rises (positive step), terminal voltage falls.
    return pulse(
        soc,
        0.0,
        current_step,
        voltage_before,
        voltage_before - resistance * current_step,
        **extra,
    )


def charge(soc, current_step, resistance, voltage_before=3.5, **extra):
    # Charge: current falls (negative step), terminal voltage rises.
    return pulse(
        soc,
        current_step,
        0.0,
        voltage_before,
        voltage_before + resistance * current_step,
        **extra,
    )


def base_request(**overrides):
    payload = {
        "pulses": [
            discharge(0.2, 1.0, 0.10, voltage_before=4.0),
            discharge(0.5, 2.0, 0.12, voltage_before=3.8),
            discharge(0.8, 1.0, 0.15, voltage_before=3.6),
        ]
    }
    payload.update(overrides)
    return payload


class InternalResistanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_exact_fit_one_pulse_per_soc(self) -> None:
        result = self.service.estimate_internal_resistance(base_request())
        self.assertEqual(result["status"], "fitted")
        curve = result["curve"]
        self.assertEqual([node["soc"] for node in curve], [0.2, 0.5, 0.8])
        for node, resistance in zip(curve, (0.10, 0.12, 0.15)):
            self.assertAlmostEqual(node["resistance_ohm"], resistance)
            self.assertEqual(node["pulse_count"], 1)
            self.assertAlmostEqual(node["weight_sum"], 1.0)
        self.assertAlmostEqual(result["recommended_r0_ohm"], 0.15)
        self.assertAlmostEqual(result["rmse_voltage_v"], 0.0)
        estimates = result["pulse_estimates"]
        self.assertEqual([item["index"] for item in estimates], [0, 1, 2])
        self.assertEqual([item["soc"] for item in estimates], [0.2, 0.5, 0.8])

    def test_pulse_estimate_fields_and_signs(self) -> None:
        result = self.service.estimate_internal_resistance(base_request())
        first = result["pulse_estimates"][0]
        self.assertAlmostEqual(first["current_change_a"], 1.0)
        self.assertAlmostEqual(first["voltage_change_v"], -0.10)
        self.assertAlmostEqual(first["resistance_ohm"], 0.10)
        self.assertAlmostEqual(first["fitted_voltage_change_v"], -0.10)
        self.assertAlmostEqual(first["residual_voltage_v"], 0.0)

    def test_charge_pulse_uses_negative_step(self) -> None:
        payload = {"pulses": [charge(0.2, 2.0, 0.15), charge(0.8, 1.0, 0.10)]}
        result = self.service.estimate_internal_resistance(payload)
        first = result["pulse_estimates"][0]
        self.assertAlmostEqual(first["current_change_a"], -2.0)
        self.assertAlmostEqual(first["voltage_change_v"], 0.30)
        self.assertAlmostEqual(first["resistance_ohm"], 0.15)
        self.assertAlmostEqual(first["fitted_voltage_change_v"], 0.30)
        self.assertAlmostEqual(result["recommended_r0_ohm"], 0.15)
        self.assertAlmostEqual(result["rmse_voltage_v"], 0.0)

    def test_duplicate_soc_merges_into_weighted_mean_knot(self) -> None:
        payload = {
            "pulses": [
                discharge(0.2, 1.0, 0.10, weight=1.0),
                discharge(0.2, 2.0, 0.15, weight=3.0),
                discharge(0.8, 1.0, 0.15),
            ]
        }
        result = self.service.estimate_internal_resistance(payload)
        curve = result["curve"]
        self.assertEqual(len(curve), 2)
        low, high = curve
        self.assertAlmostEqual(low["soc"], 0.2)
        self.assertAlmostEqual(low["resistance_ohm"], 0.1375)
        self.assertEqual(low["pulse_count"], 2)
        self.assertAlmostEqual(low["weight_sum"], 4.0)
        self.assertEqual(high["pulse_count"], 1)
        self.assertAlmostEqual(high["weight_sum"], 1.0)
        self.assertAlmostEqual(result["recommended_r0_ohm"], 0.15)

        estimates = result["pulse_estimates"]
        self.assertAlmostEqual(estimates[0]["resistance_ohm"], 0.10)
        self.assertAlmostEqual(estimates[0]["fitted_voltage_change_v"], -0.1375)
        self.assertAlmostEqual(estimates[0]["residual_voltage_v"], 0.0375)
        self.assertAlmostEqual(estimates[1]["resistance_ohm"], 0.15)
        self.assertAlmostEqual(estimates[1]["fitted_voltage_change_v"], -0.275)
        self.assertAlmostEqual(estimates[1]["residual_voltage_v"], -0.025)
        self.assertAlmostEqual(estimates[2]["residual_voltage_v"], 0.0)
        expected_rmse = math.sqrt(
            (1.0 * 0.0375**2 + 3.0 * 0.025**2) / 5.0
        )
        self.assertAlmostEqual(result["rmse_voltage_v"], expected_rmse)

    def test_pulse_estimates_follow_input_order(self) -> None:
        payload = {
            "pulses": [
                discharge(0.8, 1.0, 0.15),
                discharge(0.2, 1.0, 0.10),
                charge(0.5, 2.0, 0.12),
            ]
        }
        result = self.service.estimate_internal_resistance(payload)
        estimates = result["pulse_estimates"]
        self.assertEqual([item["index"] for item in estimates], [0, 1, 2])
        self.assertEqual([item["soc"] for item in estimates], [0.8, 0.2, 0.5])
        self.assertEqual([node["soc"] for node in result["curve"]], [0.2, 0.5, 0.8])

    def test_default_min_current_step_is_tenth_ampere(self) -> None:
        # Exactly the threshold passes; below it fails.
        ok = base_request(
            pulses=[
                discharge(0.2, 0.1, 0.10),
                discharge(0.8, 0.1, 0.12),
            ]
        )
        result = self.service.estimate_internal_resistance(ok)
        self.assertEqual(result["status"], "fitted")
        small = base_request(
            pulses=[
                discharge(0.2, 0.099, 0.10),
                discharge(0.8, 0.1, 0.12),
            ]
        )
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_internal_resistance(small)
        self.assertEqual(ctx.exception.code, "invalid_current_step")
        self.assertEqual(ctx.exception.status, 422)

    def test_custom_min_current_step_option(self) -> None:
        payload = base_request(
            pulses=[
                discharge(0.2, 0.5, 0.10),
                discharge(0.8, 0.4, 0.12),
            ],
            options={"min_current_step_a": 0.4},
        )
        result = self.service.estimate_internal_resistance(payload)
        self.assertEqual(result["status"], "fitted")
        payload["options"]["min_current_step_a"] = 0.41
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_internal_resistance(payload)
        self.assertEqual(ctx.exception.code, "invalid_current_step")

    def test_unknown_fields_are_ignored_and_request_is_not_modified(self) -> None:
        payload = base_request(
            options={"min_current_step_a": 0.05, "unknown_option": 1},
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

    def test_soc_rules(self) -> None:
        for bad in (None, -0.1, 1.1, float("nan"), float("inf"), True, "0.5"):
            self.assert_code(
                base_request(
                    pulses=[
                        discharge(0.2, 1.0, 0.10),
                        discharge(bad, 1.0, 0.12),
                    ]
                ),
                "invalid_soc",
            )
        missing = {
            "pulses": [
                discharge(0.2, 1.0, 0.10),
                {
                    "current_before_a": 0.0,
                    "current_after_a": 1.0,
                    "voltage_before_v": 4.0,
                    "voltage_after_v": 3.9,
                },
            ]
        }
        self.assert_code(missing, "invalid_soc")

    def test_current_rules(self) -> None:
        for bad in (None, float("nan"), float("inf"), True, "1"):
            self.assert_code(
                {
                    "pulses": [
                        discharge(0.2, 1.0, 0.10),
                        pulse(0.8, bad, 1.0, 4.0, 3.9),
                    ]
                },
                "invalid_current",
            )
            self.assert_code(
                {
                    "pulses": [
                        discharge(0.2, 1.0, 0.10),
                        pulse(0.8, 0.0, bad, 4.0, 3.9),
                    ]
                },
                "invalid_current",
            )
        missing_before = {
            "pulses": [
                discharge(0.2, 1.0, 0.10),
                {"current_after_a": 1.0, "voltage_before_v": 4.0,
                 "voltage_after_v": 3.9, "soc": 0.8},
            ]
        }
        self.assert_code(missing_before, "invalid_current")

    def test_voltage_rules(self) -> None:
        for bad in (None, float("nan"), float("inf"), 0.0, -1.0, False, "3.9"):
            self.assert_code(
                {
                    "pulses": [
                        discharge(0.2, 1.0, 0.10),
                        pulse(0.8, 0.0, 1.0, bad, 3.9),
                    ]
                },
                "invalid_voltage",
            )
            self.assert_code(
                {
                    "pulses": [
                        discharge(0.2, 1.0, 0.10),
                        pulse(0.8, 0.0, 1.0, 4.0, bad),
                    ]
                },
                "invalid_voltage",
            )
        missing_after = {
            "pulses": [
                discharge(0.2, 1.0, 0.10),
                {"soc": 0.8, "current_before_a": 0.0, "current_after_a": 1.0,
                 "voltage_before_v": 4.0},
            ]
        }
        self.assert_code(missing_after, "invalid_voltage")

    def test_weight_rules(self) -> None:
        for bad in (0, -1.0, float("nan"), float("inf"), True, "1"):
            self.assert_code(
                base_request(
                    pulses=[
                        discharge(0.2, 1.0, 0.10),
                        discharge(0.8, 1.0, 0.12, weight=bad),
                    ]
                ),
                "invalid_weight",
            )

    def test_options_rules(self) -> None:
        self.assert_code(base_request(options=[]), "invalid_options")
        self.assert_code(base_request(options="x"), "invalid_options")
        for bad in (0, -0.1, float("nan"), float("inf"), True, "0.1"):
            self.assert_code(
                base_request(options={"min_current_step_a": bad}),
                "invalid_options",
            )

    def test_response_direction_must_oppose_current_step(self) -> None:
        # Current and voltage both rise.
        same_up = {
            "pulses": [
                pulse(0.2, 0.0, 1.0, 4.0, 4.1),
                discharge(0.8, 1.0, 0.12),
            ]
        }
        self.assert_code(same_up, "invalid_pulse_response")
        # Current and voltage both fall.
        same_down = {
            "pulses": [
                pulse(0.2, 1.0, 0.0, 4.1, 4.0),
                discharge(0.8, 1.0, 0.12),
            ]
        }
        self.assert_code(same_down, "invalid_pulse_response")
        # Zero voltage change with a valid step: product is not negative.
        flat = {
            "pulses": [
                pulse(0.2, 0.0, 1.0, 4.0, 4.0),
                discharge(0.8, 1.0, 0.12),
            ]
        }
        self.assert_code(flat, "invalid_pulse_response")

    def test_single_unique_soc_is_insufficient_span(self) -> None:
        payload = {
            "pulses": [
                discharge(0.5, 1.0, 0.10),
                discharge(0.5, 2.0, 0.12),
            ]
        }
        self.assert_code(payload, "insufficient_soc_span")


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
            RESISTANCE_ESTIMATE_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "fitted")
        self.assertAlmostEqual(body["recommended_r0_ohm"], 0.15)
        socs = [node["soc"] for node in body["curve"]]
        self.assertEqual(socs, sorted(socs))
        self.assertEqual(len(body["pulse_estimates"]), 3)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(RESISTANCE_ESTIMATE_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(RESISTANCE_ESTIMATE_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(RESISTANCE_ESTIMATE_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_pulses_is_422(self) -> None:
        status, body = self.post(RESISTANCE_ESTIMATE_ROUTE, b"{}")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_pulses")

    def test_wrong_response_direction_is_422(self) -> None:
        payload = {
            "pulses": [
                pulse(0.2, 0.0, 1.0, 4.0, 4.1),
                discharge(0.8, 1.0, 0.12),
            ]
        }
        status, body = self.post(
            RESISTANCE_ESTIMATE_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_pulse_response")

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
