import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import LDO_EFFICIENCY_ROUTE, Handler
from powerrail.service import ApiError, Service


def curve_point(current, dropout):
    return {"output_current_a": current, "dropout_voltage_v": dropout}


def operating_point(duration=3600.0, input_voltage=5.0, requested=3.3, current=1.0):
    return {
        "duration_s": duration,
        "input_voltage_v": input_voltage,
        "requested_output_voltage_v": requested,
        "output_current_a": current,
    }


def base_request(**overrides):
    payload = {
        "operating_points": [
            operating_point(current=1.0),
            operating_point(current=2.0),
        ],
        "dropout_curve": [
            curve_point(0.0, 0.1),
            curve_point(1.0, 0.2),
            curve_point(3.0, 0.6),
        ],
    }
    payload.update(overrides)
    return payload


class LdoEfficiencyEstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_estimates_match_operating_points_in_order(self) -> None:
        result = self.service.estimate_ldo_efficiency(base_request())
        self.assertEqual(len(result["estimates"]), 2)
        for item in result["estimates"]:
            self.assertEqual(
                set(item),
                {
                    "actual_output_voltage_v",
                    "dropout_voltage_v",
                    "input_power_w",
                    "output_power_w",
                    "loss_power_w",
                    "efficiency",
                    "state",
                },
            )

    def test_regulated_point_on_curve_vertex(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(operating_points=[operating_point(current=1.0)])
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["dropout_voltage_v"], 0.2)
        self.assertAlmostEqual(item["actual_output_voltage_v"], 3.3)
        self.assertEqual(item["state"], "regulated")
        self.assertAlmostEqual(item["output_power_w"], 3.3)
        self.assertAlmostEqual(item["input_power_w"], 5.0)
        self.assertAlmostEqual(item["loss_power_w"], 1.7)
        self.assertAlmostEqual(item["efficiency"], 3.3 / 5.0)

    def test_interpolates_dropout_between_curve_points(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(operating_points=[operating_point(current=2.0)])
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["dropout_voltage_v"], 0.4)
        self.assertAlmostEqual(item["actual_output_voltage_v"], 3.3)
        self.assertEqual(item["state"], "regulated")

    def test_out_of_range_current_uses_nearest_endpoint(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(
                operating_points=[
                    operating_point(current=0.0),
                    operating_point(current=10.0),
                ]
            )
        )
        self.assertAlmostEqual(result["estimates"][0]["dropout_voltage_v"], 0.1)
        self.assertAlmostEqual(result["estimates"][1]["dropout_voltage_v"], 0.6)

    def test_dropout_state_when_ceiling_below_requested(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(
                operating_points=[
                    operating_point(input_voltage=3.5, requested=3.3, current=3.0)
                ]
            )
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["dropout_voltage_v"], 0.6)
        self.assertAlmostEqual(item["actual_output_voltage_v"], 2.9)
        self.assertEqual(item["state"], "dropout")
        self.assertAlmostEqual(item["output_power_w"], 2.9 * 3.0)
        self.assertAlmostEqual(item["input_power_w"], 3.5 * 3.0)

    def test_actual_output_floored_at_zero(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(
                dropout_curve=[curve_point(0.0, 5.0), curve_point(1.0, 6.0)],
                operating_points=[
                    operating_point(input_voltage=4.0, requested=3.3, current=1.0)
                ],
            )
        )
        item = result["estimates"][0]
        self.assertEqual(item["actual_output_voltage_v"], 0.0)
        self.assertEqual(item["state"], "dropout")
        self.assertEqual(item["output_power_w"], 0.0)
        self.assertEqual(item["efficiency"], 0.0)

    def test_boundary_ceiling_equal_to_requested_is_regulated(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(
                operating_points=[
                    operating_point(input_voltage=3.5, requested=3.3, current=1.0)
                ]
            )
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["actual_output_voltage_v"], 3.3)
        self.assertEqual(item["state"], "regulated")

    def test_quiescent_current_adds_input_power(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(
                quiescent_current_a=0.1,
                operating_points=[operating_point(current=0.0)],
            )
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["input_power_w"], 0.5)
        self.assertEqual(item["output_power_w"], 0.0)
        self.assertAlmostEqual(item["loss_power_w"], 0.5)
        self.assertEqual(item["efficiency"], 0.0)

    def test_quiescent_defaults_to_zero(self) -> None:
        without = self.service.estimate_ldo_efficiency(
            base_request(operating_points=[operating_point(current=2.0)])
        )
        with_zero = self.service.estimate_ldo_efficiency(
            base_request(
                quiescent_current_a=0.0,
                operating_points=[operating_point(current=2.0)],
            )
        )
        self.assertEqual(without["estimates"], with_zero["estimates"])

    def test_energy_totals_and_overall_efficiency(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(
                operating_points=[
                    operating_point(duration=1800.0, current=1.0),
                    operating_point(duration=1800.0, current=2.0),
                ]
            )
        )
        first = result["estimates"][0]
        second = result["estimates"][1]
        self.assertAlmostEqual(
            result["input_energy_wh"],
            first["input_power_w"] * 0.5 + second["input_power_w"] * 0.5,
        )
        self.assertAlmostEqual(result["output_energy_wh"], 4.95)
        self.assertAlmostEqual(
            result["loss_energy_wh"],
            result["input_energy_wh"] - result["output_energy_wh"],
        )
        self.assertAlmostEqual(
            result["overall_efficiency"],
            result["output_energy_wh"] / result["input_energy_wh"],
        )

    def test_overall_efficiency_zero_without_input_energy(self) -> None:
        result = self.service.estimate_ldo_efficiency(
            base_request(
                operating_points=[operating_point(current=0.0)],
            )
        )
        self.assertEqual(result["input_energy_wh"], 0.0)
        self.assertEqual(result["output_energy_wh"], 0.0)
        self.assertEqual(result["loss_energy_wh"], 0.0)
        self.assertEqual(result["overall_efficiency"], 0.0)

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request(quiescent_current_a=0.05)
        snapshot = json.loads(json.dumps(payload))
        self.service.estimate_ldo_efficiency(payload)
        self.assertEqual(payload, snapshot)


class LdoEfficiencyValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_ldo_efficiency(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5, 1.5, True):
            self.assert_code(payload, "invalid_json", status=400)

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

        for key in ("duration_s", "input_voltage_v", "requested_output_voltage_v"):
            for bad in (None, 0.0, -1.0, True, "x", float("nan"), float("inf")):
                self.assert_code(with_point(**{key: bad}), "invalid_operating_point")
        for bad in (None, -0.1, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_point(output_current_a=bad), "invalid_operating_point"
            )
        for key in (
            "duration_s",
            "input_voltage_v",
            "requested_output_voltage_v",
            "output_current_a",
        ):
            point = operating_point()
            del point[key]
            self.assert_code(
                base_request(operating_points=[point]), "invalid_operating_point"
            )
        # Zero output current is allowed.
        result = self.service.estimate_ldo_efficiency(
            with_point(output_current_a=0.0)
        )
        self.assertEqual(result["estimates"][0]["output_power_w"], 0.0)

    def test_dropout_curve_structure_rules(self) -> None:
        self.assert_code(base_request(dropout_curve=None), "invalid_dropout_curve")
        self.assert_code(base_request(dropout_curve=[]), "invalid_dropout_curve")
        self.assert_code(base_request(dropout_curve="x"), "invalid_dropout_curve")
        self.assert_code(
            base_request(dropout_curve=[curve_point(0.0, 0.1)]),
            "invalid_dropout_curve",
        )
        self.assert_code(
            base_request(dropout_curve=[None, curve_point(1.0, 0.2)]),
            "invalid_dropout_curve",
        )
        missing = {k: v for k, v in base_request().items() if k != "dropout_curve"}
        self.assert_code(missing, "invalid_dropout_curve")

    def test_dropout_curve_value_rules(self) -> None:
        for bad in (None, -0.1, True, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(
                    dropout_curve=[curve_point(bad, 0.1), curve_point(1.0, 0.2)]
                ),
                "invalid_dropout_curve",
            )
            self.assert_code(
                base_request(
                    dropout_curve=[curve_point(0.0, bad), curve_point(1.0, 0.2)]
                ),
                "invalid_dropout_curve",
            )
        # Zero dropout voltage is allowed.
        result = self.service.estimate_ldo_efficiency(
            base_request(
                dropout_curve=[curve_point(0.0, 0.0), curve_point(1.0, 0.0)],
                operating_points=[operating_point(current=0.5)],
            )
        )
        self.assertEqual(result["estimates"][0]["dropout_voltage_v"], 0.0)
        self.assertEqual(result["estimates"][0]["state"], "regulated")

    def test_dropout_curve_must_be_strictly_increasing(self) -> None:
        self.assert_code(
            base_request(
                dropout_curve=[curve_point(1.0, 0.1), curve_point(1.0, 0.2)]
            ),
            "invalid_dropout_curve",
        )
        self.assert_code(
            base_request(
                dropout_curve=[curve_point(2.0, 0.1), curve_point(1.0, 0.2)]
            ),
            "invalid_dropout_curve",
        )


class LdoEfficiencyHttpTest(unittest.TestCase):
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
            LDO_EFFICIENCY_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["estimates"]), 2)
        self.assertIn("input_energy_wh", body)
        self.assertIn("output_energy_wh", body)
        self.assertIn("loss_energy_wh", body)
        self.assertIn("overall_efficiency", body)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(LDO_EFFICIENCY_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(LDO_EFFICIENCY_ROUTE, b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(LDO_EFFICIENCY_ROUTE, b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_operating_point_is_422(self) -> None:
        payload = base_request(
            operating_points=[operating_point(input_voltage=-1.0)]
        )
        status, body = self.post(LDO_EFFICIENCY_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_operating_point")
        self.assertTrue(body["error"]["message"])

    def test_invalid_dropout_curve_is_422(self) -> None:
        payload = base_request(
            dropout_curve=[curve_point(2.0, 0.2), curve_point(1.0, 0.1)]
        )
        status, body = self.post(LDO_EFFICIENCY_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_dropout_curve")


if __name__ == "__main__":
    unittest.main()
