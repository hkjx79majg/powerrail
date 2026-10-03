import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import SOLAR_HARVEST_ROUTE, Handler
from powerrail.service import ApiError, Service


def sample(timestamp, voltage=5.0, current=2.0, acceptance=100.0):
    return {
        "timestamp_s": timestamp,
        "panel_voltage_v": voltage,
        "panel_current_a": current,
        "battery_acceptance_power_w": acceptance,
    }


def curve_point(power, efficiency):
    return {"input_power_w": power, "efficiency": efficiency}


def base_request(**overrides):
    payload = {
        "samples": [
            sample(0.0),
            sample(1800.0),
            sample(3600.0),
        ],
        "harvester": {
            "max_input_power_w": 20.0,
            "efficiency_curve": [
                curve_point(0.0, 0.8),
                curve_point(10.0, 0.9),
                curve_point(20.0, 0.95),
            ],
        },
    }
    payload.update(overrides)
    return payload


class SolarHarvestEstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_estimates_match_samples_in_order_with_timestamps(self) -> None:
        result = self.service.estimate_solar_harvest(base_request())
        self.assertEqual(len(result["estimates"]), 3)
        self.assertEqual(
            [item["timestamp_s"] for item in result["estimates"]],
            [0.0, 1800.0, 3600.0],
        )
        for item in result["estimates"]:
            self.assertEqual(
                set(item),
                {
                    "timestamp_s",
                    "available_power_w",
                    "harvester_input_power_w",
                    "efficiency",
                    "converted_power_w",
                    "harvested_power_w",
                    "conversion_loss_power_w",
                    "curtailed_power_w",
                    "rejected_power_w",
                },
            )

    def test_power_math_on_curve_point(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(samples=[sample(0.0), sample(60.0)])
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["available_power_w"], 10.0)
        self.assertAlmostEqual(item["harvester_input_power_w"], 10.0)
        self.assertAlmostEqual(item["efficiency"], 0.9)
        self.assertAlmostEqual(item["converted_power_w"], 9.0)
        self.assertAlmostEqual(item["harvested_power_w"], 9.0)
        self.assertAlmostEqual(item["conversion_loss_power_w"], 1.0)
        self.assertAlmostEqual(item["curtailed_power_w"], 0.0)
        self.assertAlmostEqual(item["rejected_power_w"], 0.0)

    def test_interpolation_between_curve_points(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(samples=[sample(0.0, voltage=5.0, current=1.0), sample(60.0)])
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["available_power_w"], 5.0)
        self.assertAlmostEqual(item["efficiency"], 0.85)
        self.assertAlmostEqual(item["converted_power_w"], 4.25)

    def test_out_of_range_input_uses_nearest_endpoint(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                harvester={
                    "max_input_power_w": 100.0,
                    "efficiency_curve": [
                        curve_point(5.0, 0.8),
                        curve_point(10.0, 0.9),
                    ],
                },
                samples=[
                    sample(0.0, voltage=1.0, current=1.0),
                    sample(60.0, voltage=10.0, current=5.0),
                ],
            )
        )
        self.assertAlmostEqual(result["estimates"][0]["efficiency"], 0.8)
        self.assertAlmostEqual(result["estimates"][1]["efficiency"], 0.9)

    def test_max_input_power_curtails_panel_power(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[sample(0.0, voltage=10.0, current=3.0), sample(60.0)]
            )
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["available_power_w"], 30.0)
        self.assertAlmostEqual(item["harvester_input_power_w"], 20.0)
        self.assertAlmostEqual(item["curtailed_power_w"], 10.0)
        self.assertAlmostEqual(item["efficiency"], 0.95)
        self.assertAlmostEqual(item["converted_power_w"], 19.0)

    def test_battery_acceptance_rejects_converted_power(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, acceptance=5.0),
                    sample(60.0),
                ]
            )
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["converted_power_w"], 9.0)
        self.assertAlmostEqual(item["harvested_power_w"], 5.0)
        self.assertAlmostEqual(item["rejected_power_w"], 4.0)

    def test_energy_totals_use_trapezoidal_integration(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=5.0, current=2.0),
                    sample(3600.0, voltage=5.0, current=4.0),
                ]
            )
        )
        first, second = result["estimates"]
        for power_key, energy_key in (
            ("available_power_w", "available_energy_wh"),
            ("harvested_power_w", "harvested_energy_wh"),
            ("conversion_loss_power_w", "conversion_loss_energy_wh"),
            ("curtailed_power_w", "curtailed_energy_wh"),
            ("rejected_power_w", "rejected_energy_wh"),
        ):
            self.assertAlmostEqual(
                result[energy_key],
                (first[power_key] + second[power_key]) / 2.0 * 3600.0 / 3600.0,
            )
        self.assertAlmostEqual(result["available_energy_wh"], 15.0)
        self.assertAlmostEqual(
            result["overall_efficiency"],
            result["harvested_energy_wh"] / result["available_energy_wh"],
        )

    def test_overall_efficiency_zero_without_available_energy(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=0.0, current=0.0),
                    sample(60.0, voltage=0.0, current=0.0),
                ]
            )
        )
        self.assertEqual(result["available_energy_wh"], 0.0)
        self.assertEqual(result["harvested_energy_wh"], 0.0)
        self.assertEqual(result["overall_efficiency"], 0.0)

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.estimate_solar_harvest(payload)
        self.assertEqual(payload, snapshot)


class SolarHarvestValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_error(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_solar_harvest(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5, 1.5, True):
            self.assert_error(payload, "invalid_json", status=400)

    def test_samples_must_have_at_least_two_objects(self) -> None:
        self.assert_error(base_request(samples=None), "invalid_samples")
        self.assert_error(base_request(samples="x"), "invalid_samples")
        self.assert_error(base_request(samples=[]), "invalid_samples")
        self.assert_error(base_request(samples=[sample(0.0)]), "invalid_samples")
        self.assert_error(
            base_request(samples=[sample(0.0), "x"]), "invalid_samples"
        )

    def test_timestamp_rules(self) -> None:
        for bad in (None, "x", True, float("nan"), float("inf")):
            self.assert_error(
                base_request(samples=[sample(bad), sample(60.0)]),
                "invalid_samples",
            )
        self.assert_error(
            base_request(samples=[sample(60.0), sample(60.0)]),
            "invalid_samples",
        )
        self.assert_error(
            base_request(samples=[sample(60.0), sample(30.0)]),
            "invalid_samples",
        )

    def test_panel_and_acceptance_rules(self) -> None:
        for key in ("panel_voltage_v", "panel_current_a", "battery_acceptance_power_w"):
            for bad in (None, -0.1, "x", True, float("nan"), float("inf")):
                broken = sample(0.0)
                broken[key] = bad
                self.assert_error(
                    base_request(samples=[broken, sample(60.0)]),
                    "invalid_samples",
                )

    def test_harvester_must_be_an_object(self) -> None:
        for bad in (None, [], "x", 5):
            self.assert_error(base_request(harvester=bad), "invalid_harvester_config")

    def test_max_input_power_rules(self) -> None:
        for bad in (None, 0.0, -1.0, "x", True, float("nan"), float("inf")):
            self.assert_error(
                base_request(
                    harvester={
                        "max_input_power_w": bad,
                        "efficiency_curve": [
                            curve_point(0.0, 0.8),
                            curve_point(10.0, 0.9),
                        ],
                    }
                ),
                "invalid_harvester_config",
            )

    def test_efficiency_curve_structure_rules(self) -> None:
        harvester = base_request()["harvester"]
        for bad_curve in (None, "x", [], [curve_point(0.0, 0.8)], [curve_point(0.0, 0.8), "x"]):
            self.assert_error(
                base_request(harvester={**harvester, "efficiency_curve": bad_curve}),
                "invalid_harvester_config",
            )

    def test_efficiency_curve_value_rules(self) -> None:
        harvester = base_request()["harvester"]
        for bad in (None, -0.1, "x", True, float("nan"), float("inf")):
            self.assert_error(
                base_request(
                    harvester={
                        **harvester,
                        "efficiency_curve": [
                            curve_point(bad, 0.8),
                            curve_point(10.0, 0.9),
                        ],
                    }
                ),
                "invalid_harvester_config",
            )
        for bad in (None, 0.0, -0.1, 1.1, "x", True, float("nan"), float("inf")):
            self.assert_error(
                base_request(
                    harvester={
                        **harvester,
                        "efficiency_curve": [
                            curve_point(0.0, bad),
                            curve_point(10.0, 0.9),
                        ],
                    }
                ),
                "invalid_harvester_config",
            )

    def test_efficiency_curve_must_be_strictly_increasing(self) -> None:
        harvester = base_request()["harvester"]
        for curve in (
            [curve_point(10.0, 0.8), curve_point(10.0, 0.9)],
            [curve_point(20.0, 0.8), curve_point(10.0, 0.9)],
        ):
            self.assert_error(
                base_request(harvester={**harvester, "efficiency_curve": curve}),
                "invalid_harvester_config",
            )


class SolarHarvestHttpTest(unittest.TestCase):
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

    def post(self, raw):
        request = urllib.request.Request(
            self.base + SOLAR_HARVEST_ROUTE,
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
        status, body = self.post(json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(len(body["estimates"]), 3)
        self.assertIn("available_energy_wh", body)
        self.assertIn("harvested_energy_wh", body)
        self.assertIn("conversion_loss_energy_wh", body)
        self.assertIn("curtailed_energy_wh", body)
        self.assertIn("rejected_energy_wh", body)
        self.assertIn("overall_efficiency", body)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_samples_is_422(self) -> None:
        payload = base_request(samples=[sample(0.0)])
        status, body = self.post(json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_invalid_harvester_config_is_422(self) -> None:
        payload = base_request()
        payload["harvester"]["max_input_power_w"] = -1.0
        status, body = self.post(json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_harvester_config")


if __name__ == "__main__":
    unittest.main()
