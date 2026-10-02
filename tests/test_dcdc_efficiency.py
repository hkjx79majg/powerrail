import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import DCDC_EFFICIENCY_ROUTE, Handler
from powerrail.service import ApiError, Service


def curve_point(current, efficiency):
    return {"output_current_a": current, "efficiency": efficiency}


def operating_point(duration=3600.0, voltage=5.0, current=2.0):
    return {
        "duration_s": duration,
        "output_voltage_v": voltage,
        "output_current_a": current,
    }


def base_request(**overrides):
    payload = {
        "input_voltage_v": 12.0,
        "operating_points": [
            operating_point(current=1.0),
            operating_point(current=2.0),
        ],
        "efficiency_curve": [
            curve_point(0.0, 0.8),
            curve_point(2.0, 0.9),
            curve_point(5.0, 0.95),
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
        for item in result["estimates"]:
            self.assertEqual(
                set(item),
                {"input_power_w", "output_power_w", "loss_power_w", "efficiency"},
            )

    def test_power_math_on_curve_point(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(operating_points=[operating_point(current=2.0)])
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["output_power_w"], 10.0)
        self.assertAlmostEqual(item["input_power_w"], 10.0 / 0.9)
        self.assertAlmostEqual(item["loss_power_w"], 10.0 / 0.9 - 10.0)
        self.assertAlmostEqual(item["efficiency"], 0.9)

    def test_interpolation_between_curve_points(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(operating_points=[operating_point(current=1.0)])
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["output_power_w"], 5.0)
        self.assertAlmostEqual(item["input_power_w"], 5.0 / 0.85)
        self.assertAlmostEqual(item["efficiency"], 0.85)

    def test_out_of_range_current_uses_nearest_endpoint(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(
                efficiency_curve=[
                    curve_point(1.0, 0.8),
                    curve_point(2.0, 0.9),
                    curve_point(5.0, 0.95),
                ],
                operating_points=[
                    operating_point(current=0.5),
                    operating_point(current=10.0),
                ],
            )
        )
        self.assertAlmostEqual(result["estimates"][0]["efficiency"], 0.8)
        self.assertAlmostEqual(
            result["estimates"][0]["input_power_w"], 2.5 / 0.8
        )
        self.assertAlmostEqual(result["estimates"][1]["efficiency"], 0.95)
        self.assertAlmostEqual(
            result["estimates"][1]["input_power_w"], 50.0 / 0.95
        )

    def test_zero_input_power_reports_zero_efficiency_and_loss(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(operating_points=[operating_point(current=0.0)])
        )
        item = result["estimates"][0]
        self.assertEqual(item["input_power_w"], 0.0)
        self.assertEqual(item["output_power_w"], 0.0)
        self.assertEqual(item["loss_power_w"], 0.0)
        self.assertEqual(item["efficiency"], 0.0)

    def test_quiescent_current_adds_input_power(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(
                quiescent_current_a=0.1,
                operating_points=[operating_point(current=0.0)],
            )
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["input_power_w"], 1.2)
        self.assertEqual(item["output_power_w"], 0.0)
        self.assertAlmostEqual(item["loss_power_w"], 1.2)
        self.assertEqual(item["efficiency"], 0.0)

    def test_quiescent_defaults_to_zero(self) -> None:
        without = self.service.estimate_dcdc_efficiency(
            base_request(operating_points=[operating_point(current=2.0)])
        )
        with_zero = self.service.estimate_dcdc_efficiency(
            base_request(
                quiescent_current_a=0.0,
                operating_points=[operating_point(current=2.0)],
            )
        )
        self.assertEqual(without["estimates"], with_zero["estimates"])

    def test_energy_totals_and_overall_efficiency(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(
                operating_points=[
                    operating_point(duration=1800.0, current=2.0),
                    operating_point(duration=1800.0, current=1.0),
                ]
            )
        )
        first = result["estimates"][0]
        second = result["estimates"][1]
        self.assertAlmostEqual(
            result["input_energy_wh"],
            first["input_power_w"] * 0.5 + second["input_power_w"] * 0.5,
        )
        self.assertAlmostEqual(result["output_energy_wh"], 7.5)
        self.assertAlmostEqual(
            result["loss_energy_wh"],
            result["input_energy_wh"] - result["output_energy_wh"],
        )
        self.assertAlmostEqual(
            result["overall_efficiency"],
            result["output_energy_wh"] / result["input_energy_wh"],
        )

    def test_overall_efficiency_zero_without_throughput(self) -> None:
        result = self.service.estimate_dcdc_efficiency(
            base_request(operating_points=[operating_point(current=0.0)])
        )
        self.assertEqual(result["input_energy_wh"], 0.0)
        self.assertEqual(result["output_energy_wh"], 0.0)
        self.assertEqual(result["loss_energy_wh"], 0.0)
        self.assertEqual(result["overall_efficiency"], 0.0)

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request(quiescent_current_a=0.05)
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
        for bad in (None, 0.0, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(base_request(input_voltage_v=bad), "invalid_input_voltage")
        missing = {k: v for k, v in base_request().items() if k != "input_voltage_v"}
        self.assert_code(missing, "invalid_input_voltage")

    def test_quiescent_current_rules(self) -> None:
        for bad in (-0.1, False, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(quiescent_current_a=bad),
                "invalid_quiescent_current",
            )

    def test_operating_points_rules(self) -> None:
        self.assert_code(base_request(operating_points=[]), "invalid_operating_points")
        self.assert_code(base_request(operating_points=None), "invalid_operating_points")
        self.assert_code(base_request(operating_points="x"), "invalid_operating_points")
        self.assert_code(base_request(operating_points=[1]), "invalid_operating_points")
        self.assert_code(base_request(operating_points=[None]), "invalid_operating_points")
        missing = {
            k: v for k, v in base_request().items() if k != "operating_points"
        }
        self.assert_code(missing, "invalid_operating_points")

    def test_operating_point_field_rules(self) -> None:
        def with_point(**fields):
            point = operating_point()
            point.update(fields)
            return base_request(operating_points=[point])

        for bad in (None, 0.0, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(with_point(duration_s=bad), "invalid_operating_point")
            self.assert_code(with_point(output_voltage_v=bad), "invalid_operating_point")
        for bad in (None, -0.1, True, "x", float("nan"), float("inf")):
            self.assert_code(with_point(output_current_a=bad), "invalid_operating_point")
        for key in ("duration_s", "output_voltage_v", "output_current_a"):
            point = operating_point()
            del point[key]
            self.assert_code(
                base_request(operating_points=[point]), "invalid_operating_point"
            )
        # Zero output current is allowed.
        result = self.service.estimate_dcdc_efficiency(
            with_point(output_current_a=0.0)
        )
        self.assertEqual(result["estimates"][0]["output_power_w"], 0.0)

    def test_efficiency_curve_rules(self) -> None:
        self.assert_code(
            base_request(efficiency_curve=None), "invalid_efficiency_curve"
        )
        self.assert_code(base_request(efficiency_curve=[]), "invalid_efficiency_curve")
        self.assert_code(
            base_request(efficiency_curve="x"), "invalid_efficiency_curve"
        )
        self.assert_code(
            base_request(efficiency_curve=[curve_point(0.0, 0.8)]),
            "invalid_efficiency_curve",
        )
        self.assert_code(
            base_request(efficiency_curve=[None, curve_point(2.0, 0.9)]),
            "invalid_efficiency_curve",
        )
        missing = {
            k: v for k, v in base_request().items() if k != "efficiency_curve"
        }
        self.assert_code(missing, "invalid_efficiency_curve")

    def test_efficiency_curve_value_rules(self) -> None:
        for bad in (None, -0.1, True, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(
                    efficiency_curve=[
                        curve_point(bad, 0.8),
                        curve_point(2.0, 0.9),
                    ]
                ),
                "invalid_efficiency_curve",
            )
        for bad in (None, 0.0, -0.1, 1.1, True, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(
                    efficiency_curve=[
                        curve_point(0.0, bad),
                        curve_point(2.0, 0.9),
                    ]
                ),
                "invalid_efficiency_curve",
            )
        # Efficiency of exactly 1 is allowed.
        result = self.service.estimate_dcdc_efficiency(
            base_request(
                efficiency_curve=[
                    curve_point(0.0, 0.9),
                    curve_point(2.0, 1.0),
                ]
            )
        )
        self.assertAlmostEqual(result["estimates"][1]["input_power_w"], 10.0)

    def test_efficiency_curve_must_be_strictly_increasing(self) -> None:
        self.assert_code(
            base_request(
                efficiency_curve=[
                    curve_point(2.0, 0.8),
                    curve_point(2.0, 0.9),
                ]
            ),
            "invalid_efficiency_curve",
        )
        self.assert_code(
            base_request(
                efficiency_curve=[
                    curve_point(3.0, 0.8),
                    curve_point(2.0, 0.9),
                ]
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
        self.assertIn("input_energy_wh", body)
        self.assertIn("output_energy_wh", body)
        self.assertIn("loss_energy_wh", body)
        self.assertIn("overall_efficiency", body)

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
        status, body = self.post(
            DCDC_EFFICIENCY_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_input_voltage")
        self.assertTrue(body["error"]["message"])

    def test_invalid_efficiency_curve_is_422(self) -> None:
        payload = base_request(
            efficiency_curve=[curve_point(2.0, 0.9), curve_point(1.0, 0.8)]
        )
        status, body = self.post(
            DCDC_EFFICIENCY_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_efficiency_curve")


if __name__ == "__main__":
    unittest.main()
