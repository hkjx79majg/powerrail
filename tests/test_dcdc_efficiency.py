import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import DCDC_EFFICIENCY_ROUTE, Handler
from powerrail.service import ApiError, Service


def operating_point(duration, voltage, current):
    return {
        "duration_s": duration,
        "output_voltage_v": voltage,
        "output_current_a": current,
    }


def curve_point(current, efficiency):
    return {"output_current_a": current, "efficiency": efficiency}


def base_request(**overrides):
    payload = {
        "input_voltage_v": 12.0,
        "operating_points": [
            operating_point(600.0, 5.0, 1.0),
            operating_point(1200.0, 5.0, 3.0),
        ],
        "efficiency_curve": [
            curve_point(0.0, 0.8),
            curve_point(2.0, 0.9),
            curve_point(4.0, 0.85),
        ],
    }
    payload.update(overrides)
    return payload


class DcdcEfficiencyEstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_estimates_match_operating_points_in_order(self) -> None:
        result = self.service.estimate_dcdc_efficiency(base_request())
        self.assertEqual(len(result["estimates"]), 2)
        first, second = result["estimates"]
        # Point 1: out 5 W, eff 0.85 (midpoint of 0.8/0.9), in = 5/0.85.
        self.assertAlmostEqual(first["output_power_w"], 5.0)
        self.assertAlmostEqual(first["input_power_w"], 5.0 / 0.85)
        self.assertAlmostEqual(
            first["loss_power_w"], first["input_power_w"] - first["output_power_w"]
        )
        self.assertAlmostEqual(first["efficiency"], 0.85)
        # Point 2: out 15 W, eff 0.875 (midpoint of 0.9/0.85), in = 15/0.875.
        self.assertAlmostEqual(second["output_power_w"], 15.0)
        self.assertAlmostEqual(second["input_power_w"], 15.0 / 0.875)
        self.assertAlmostEqual(second["efficiency"], 0.875)

    def test_quiescent_current_adds_input_power(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(quiescent_current_a=0.1)
        )
        first = result["estimates"][0]
        # Quiescent draw: 12 V * 0.1 A = 1.2 W on top of converted power.
        self.assertAlmostEqual(first["input_power_w"], 5.0 / 0.85 + 1.2)
        self.assertAlmostEqual(first["efficiency"], 5.0 / (5.0 / 0.85 + 1.2))

    def test_out_of_range_current_clamps_to_nearest_endpoint(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(
                operating_points=[
                    operating_point(60.0, 5.0, 10.0),
                    operating_point(60.0, 5.0, 0.0),
                ]
            )
        )
        above, below = result["estimates"]
        # 10 A is above the 4 A endpoint -> eff 0.85; 0 A -> first point 0.8.
        self.assertAlmostEqual(above["input_power_w"], 50.0 / 0.85)
        self.assertAlmostEqual(above["efficiency"], 0.85)
        self.assertAlmostEqual(below["input_power_w"], 0.0)
        self.assertAlmostEqual(below["efficiency"], 0.0)

    def test_zero_output_current_with_quiescent_draw(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(
                quiescent_current_a=0.05,
                operating_points=[operating_point(3600.0, 5.0, 0.0)],
            )
        )
        estimate = result["estimates"][0]
        self.assertAlmostEqual(estimate["output_power_w"], 0.0)
        self.assertAlmostEqual(estimate["input_power_w"], 0.6)
        self.assertAlmostEqual(estimate["loss_power_w"], 0.6)
        self.assertAlmostEqual(estimate["efficiency"], 0.0)

    def test_energy_totals_and_overall_efficiency(self) -> None:
        result = self.service.estimate_dcdc_efficiency(base_request())
        first_in = 5.0 / 0.85
        second_in = 15.0 / 0.875
        expected_in = first_in * 600.0 / 3600.0 + second_in * 1200.0 / 3600.0
        expected_out = 5.0 * 600.0 / 3600.0 + 15.0 * 1200.0 / 3600.0
        self.assertAlmostEqual(result["input_energy_wh"], expected_in)
        self.assertAlmostEqual(result["output_energy_wh"], expected_out)
        self.assertAlmostEqual(
            result["loss_energy_wh"], expected_in - expected_out
        )
        self.assertAlmostEqual(
            result["overall_efficiency"], expected_out / expected_in
        )

    def test_zero_input_energy_yields_zero_overall_efficiency(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(operating_points=[operating_point(60.0, 5.0, 0.0)])
        )
        self.assertAlmostEqual(result["input_energy_wh"], 0.0)
        self.assertAlmostEqual(result["output_energy_wh"], 0.0)
        self.assertAlmostEqual(result["loss_energy_wh"], 0.0)
        self.assertAlmostEqual(result["overall_efficiency"], 0.0)

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request(quiescent_current_a=0.02)
        snapshot = json.loads(json.dumps(payload))
        self.service.estimate_dcdc_efficiency(payload)
        self.assertEqual(payload, snapshot)


class DcdcEfficiencyValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_dcdc_efficiency(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5, 1.5, True):
            self.assert_code(payload, "invalid_json", status=400)

    def test_input_voltage_rules(self) -> None:
        for bad in (None, 0.0, -12.0, True, "12", float("nan"), float("inf")):
            self.assert_code(
                base_request(input_voltage_v=bad), "invalid_input_voltage"
            )
        missing = {k: v for k, v in base_request().items() if k != "input_voltage_v"}
        self.assert_code(missing, "invalid_input_voltage")

    def test_quiescent_current_rules(self) -> None:
        for bad in (-0.1, True, "0", float("nan"), float("inf")):
            self.assert_code(
                base_request(quiescent_current_a=bad), "invalid_quiescent_current"
            )
        # Omitted and zero are allowed.
        for payload in (base_request(), base_request(quiescent_current_a=0.0)):
            result = self.service.estimate_dcdc_efficiency(payload)
            self.assertEqual(len(result["estimates"]), 2)

    def test_operating_points_rules(self) -> None:
        self.assert_code(base_request(operating_points=[]), "invalid_operating_points")
        self.assert_code(base_request(operating_points=None), "invalid_operating_points")
        self.assert_code(base_request(operating_points="x"), "invalid_operating_points")
        self.assert_code(base_request(operating_points=[1]), "invalid_operating_points")
        self.assert_code(
            base_request(operating_points=[None]), "invalid_operating_points"
        )
        missing = {
            k: v for k, v in base_request().items() if k != "operating_points"
        }
        self.assert_code(missing, "invalid_operating_points")

    def test_operating_point_field_rules(self) -> None:
        def with_point(**fields):
            point = operating_point(60.0, 5.0, 1.0)
            point.update(fields)
            return base_request(operating_points=[point])

        for bad in (None, 0.0, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(with_point(duration_s=bad), "invalid_operating_point")
            self.assert_code(
                with_point(output_voltage_v=bad), "invalid_operating_point"
            )
        for bad in (None, -0.5, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_point(output_current_a=bad), "invalid_operating_point"
            )
        for key in ("duration_s", "output_voltage_v", "output_current_a"):
            point = operating_point(60.0, 5.0, 1.0)
            del point[key]
            self.assert_code(
                base_request(operating_points=[point]), "invalid_operating_point"
            )
        # Zero output current is allowed.
        result = self.service.estimate_dcdc_efficiency(
            with_point(output_current_a=0.0)
        )
        self.assertEqual(len(result["estimates"]), 1)

    def test_efficiency_curve_rules(self) -> None:
        self.assert_code(base_request(efficiency_curve=None), "invalid_efficiency_curve")
        self.assert_code(base_request(efficiency_curve=[]), "invalid_efficiency_curve")
        self.assert_code(
            base_request(efficiency_curve=[curve_point(0.0, 0.9)]),
            "invalid_efficiency_curve",
        )
        self.assert_code(
            base_request(efficiency_curve="x"), "invalid_efficiency_curve"
        )
        self.assert_code(
            base_request(efficiency_curve=[1, 2]), "invalid_efficiency_curve"
        )
        missing = {
            k: v for k, v in base_request().items() if k != "efficiency_curve"
        }
        self.assert_code(missing, "invalid_efficiency_curve")

    def test_efficiency_curve_point_rules(self) -> None:
        def with_curve(*points):
            return base_request(efficiency_curve=list(points))

        good = curve_point(2.0, 0.9)
        for bad_current in (None, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_curve(curve_point(bad_current, 0.8), good),
                "invalid_efficiency_curve",
            )
        for bad_eff in (None, 0.0, -0.1, 1.1, True, "x", float("nan")):
            self.assert_code(
                with_curve(curve_point(0.0, bad_eff), good),
                "invalid_efficiency_curve",
            )
        # Efficiency of exactly 1 is allowed.
        result = self.service.estimate_dcdc_efficiency(
            with_curve(curve_point(0.0, 1.0), curve_point(2.0, 1.0))
        )
        self.assertAlmostEqual(result["estimates"][0]["efficiency"], 1.0)

    def test_efficiency_curve_currents_must_strictly_increase(self) -> None:
        self.assert_code(
            base_request(
                efficiency_curve=[curve_point(1.0, 0.8), curve_point(1.0, 0.9)]
            ),
            "invalid_efficiency_curve",
        )
        self.assert_code(
            base_request(
                efficiency_curve=[curve_point(2.0, 0.8), curve_point(1.0, 0.9)]
            ),
            "invalid_efficiency_curve",
        )


class DcdcEfficiencyHttpTest(unittest.TestCase):
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
            DCDC_EFFICIENCY_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["estimates"]), 2)
        for key in (
            "input_energy_wh",
            "output_energy_wh",
            "loss_energy_wh",
            "overall_efficiency",
        ):
            self.assertIn(key, body)
        for key in (
            "input_power_w",
            "output_power_w",
            "loss_power_w",
            "efficiency",
        ):
            self.assertIn(key, body["estimates"][0])

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(DCDC_EFFICIENCY_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(DCDC_EFFICIENCY_ROUTE, b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(DCDC_EFFICIENCY_ROUTE, b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_input_voltage_is_422(self) -> None:
        payload = base_request(input_voltage_v=-1.0)
        status, body = self.post(DCDC_EFFICIENCY_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_input_voltage")
        self.assertTrue(body["error"]["message"])

    def test_invalid_efficiency_curve_is_422(self) -> None:
        payload = base_request(
            efficiency_curve=[curve_point(1.0, 0.8), curve_point(1.0, 0.9)]
        )
        status, body = self.post(DCDC_EFFICIENCY_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_efficiency_curve")

    def test_unknown_post_path_is_still_404(self) -> None:
        status, body = self.post("/v1/power/dcdc/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
