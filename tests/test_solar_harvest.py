import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import SOLAR_HARVEST_ROUTE, Handler
from powerrail.service import ApiError, Service


def curve_point(input_power, efficiency):
    return {"input_power_w": input_power, "efficiency": efficiency}


def sample(timestamp, voltage=10.0, current=2.0, acceptance=20.0):
    return {
        "timestamp_s": timestamp,
        "panel_voltage_v": voltage,
        "panel_current_a": current,
        "battery_acceptance_power_w": acceptance,
    }


def base_request(**overrides):
    payload = {
        "samples": [
            sample(0.0),
            sample(3600.0, voltage=12.0, current=3.0),
        ],
        "harvester": {
            "max_input_power_w": 25.0,
            "efficiency_curve": [
                curve_point(0.0, 0.8),
                curve_point(10.0, 0.9),
                curve_point(30.0, 0.95),
            ],
        },
    }
    payload.update(overrides)
    return payload


class SolarHarvestEstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_estimates_match_samples_in_order(self) -> None:
        result = self.service.estimate_solar_harvest(base_request())
        self.assertEqual(len(result["estimates"]), 2)
        self.assertEqual(
            [item["timestamp_s"] for item in result["estimates"]], [0.0, 3600.0]
        )
        for item in result["estimates"]:
            self.assertEqual(
                set(item),
                {
                    "timestamp_s",
                    "available_power_w",
                    "harvester_input_power_w",
                    "converted_power_w",
                    "harvested_power_w",
                    "conversion_loss_power_w",
                    "curtailed_power_w",
                    "rejected_power_w",
                },
            )

    def test_power_math_below_input_cap(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=10.0, current=1.0),
                    sample(10.0, voltage=10.0, current=1.0),
                ]
            )
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["available_power_w"], 10.0)
        self.assertAlmostEqual(item["harvester_input_power_w"], 10.0)
        self.assertAlmostEqual(item["converted_power_w"], 9.0)
        self.assertAlmostEqual(item["harvested_power_w"], 9.0)
        self.assertAlmostEqual(item["conversion_loss_power_w"], 1.0)
        self.assertEqual(item["curtailed_power_w"], 0.0)
        self.assertEqual(item["rejected_power_w"], 0.0)

    def test_input_capped_at_max_input_power(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=20.0, current=2.0),
                    sample(10.0, voltage=20.0, current=2.0),
                ]
            )
        )
        item = result["estimates"][0]
        self.assertAlmostEqual(item["available_power_w"], 40.0)
        self.assertAlmostEqual(item["harvester_input_power_w"], 25.0)
        self.assertAlmostEqual(item["curtailed_power_w"], 15.0)
        # 25 W sits between 10 W @ 0.9 and 30 W @ 0.95 -> 0.9375
        self.assertAlmostEqual(item["converted_power_w"], 25.0 * 0.9375)
        self.assertAlmostEqual(
            item["conversion_loss_power_w"], 25.0 * (1.0 - 0.9375)
        )

    def test_out_of_range_input_uses_nearest_endpoint(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=10.0, current=0.0, acceptance=1.0),
                    sample(10.0, voltage=10.0, current=4.0, acceptance=1.0),
                ],
                harvester={
                    "max_input_power_w": 25.0,
                    "efficiency_curve": [
                        curve_point(0.0, 0.8),
                        curve_point(10.0, 0.9),
                        curve_point(20.0, 0.95),
                    ],
                },
            )
        )
        first, second = result["estimates"]
        self.assertAlmostEqual(first["converted_power_w"], 0.0)
        self.assertAlmostEqual(second["harvester_input_power_w"], 25.0)
        self.assertAlmostEqual(second["converted_power_w"], 25.0 * 0.95)

    def test_battery_acceptance_caps_harvested_power(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=10.0, current=2.0, acceptance=5.0),
                    sample(10.0, voltage=10.0, current=2.0, acceptance=5.0),
                ]
            )
        )
        item = result["estimates"][0]
        # 20 W input at 0.925 -> 18.5 W converted, battery takes 5 W.
        self.assertAlmostEqual(item["converted_power_w"], 18.5)
        self.assertAlmostEqual(item["harvested_power_w"], 5.0)
        self.assertAlmostEqual(item["rejected_power_w"], 13.5)

    def test_zero_panel_power(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=0.0, current=0.0),
                    sample(10.0, voltage=0.0, current=0.0),
                ]
            )
        )
        for item in result["estimates"]:
            self.assertEqual(item["available_power_w"], 0.0)
            self.assertEqual(item["harvested_power_w"], 0.0)
            self.assertEqual(item["conversion_loss_power_w"], 0.0)
            self.assertEqual(item["curtailed_power_w"], 0.0)
            self.assertEqual(item["rejected_power_w"], 0.0)
        self.assertEqual(result["overall_efficiency"], 0.0)

    def test_trapezoidal_energy_totals(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=10.0, current=1.0),
                    sample(1800.0, voltage=10.0, current=2.0),
                    sample(3600.0, voltage=10.0, current=2.0),
                ]
            )
        )
        available = [
            item["available_power_w"] for item in result["estimates"]
        ]
        harvested = [
            item["harvested_power_w"] for item in result["estimates"]
        ]
        expected_available = (
            (available[0] + available[1]) / 2.0 * 0.5
            + (available[1] + available[2]) / 2.0 * 0.5
        )
        expected_harvested = (
            (harvested[0] + harvested[1]) / 2.0 * 0.5
            + (harvested[1] + harvested[2]) / 2.0 * 0.5
        )
        self.assertAlmostEqual(result["available_energy_wh"], expected_available)
        self.assertAlmostEqual(result["harvested_energy_wh"], expected_harvested)
        self.assertAlmostEqual(
            result["overall_efficiency"], expected_harvested / expected_available
        )
        total_loss_terms = (
            result["harvested_energy_wh"]
            + result["conversion_loss_energy_wh"]
            + result["curtailed_energy_wh"]
        )
        self.assertAlmostEqual(total_loss_terms, result["available_energy_wh"])
        self.assertEqual(
            result["rejected_energy_wh"]
            + result["harvested_energy_wh"]
            + result["conversion_loss_energy_wh"]
            + result["curtailed_energy_wh"],
            result["available_energy_wh"],
        )

    def test_timestamp_echoed_verbatim(self) -> None:
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(100),
                    sample(200),
                ]
            )
        )
        self.assertEqual(
            [item["timestamp_s"] for item in result["estimates"]], [100, 200]
        )

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.estimate_solar_harvest(payload)
        self.assertEqual(payload, snapshot)


class SolarHarvestValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_solar_harvest(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5, 1.5, True):
            self.assert_code(payload, "invalid_json", status=400)

    def test_samples_rules(self) -> None:
        self.assert_code(base_request(samples=None), "invalid_samples")
        self.assert_code(base_request(samples=[]), "invalid_samples")
        self.assert_code(base_request(samples="x"), "invalid_samples")
        self.assert_code(base_request(samples=[sample(0.0)]), "invalid_samples")
        self.assert_code(base_request(samples=[1, sample(1.0)]), "invalid_samples")
        self.assert_code(base_request(samples=[None, sample(1.0)]), "invalid_samples")
        missing = {k: v for k, v in base_request().items() if k != "samples"}
        self.assert_code(missing, "invalid_samples")

    def test_timestamp_rules(self) -> None:
        def with_sample(**fields):
            point = sample(1.0)
            point.update(fields)
            return [sample(0.0), point]

        for bad in (None, True, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(samples=with_sample(timestamp_s=bad)), "invalid_samples"
            )
        missing_point = sample(1.0)
        del missing_point["timestamp_s"]
        self.assert_code(
            base_request(samples=[sample(0.0), missing_point]), "invalid_samples"
        )
        self.assert_code(
            base_request(samples=[sample(1.0), sample(1.0)]), "invalid_samples"
        )
        self.assert_code(
            base_request(samples=[sample(2.0), sample(1.0)]), "invalid_samples"
        )

    def test_panel_reading_rules(self) -> None:
        def with_point(**fields):
            point = sample(1.0)
            point.update(fields)
            return [sample(0.0), point]

        for key in ("panel_voltage_v", "panel_current_a", "battery_acceptance_power_w"):
            for bad in (None, -0.1, True, "x", float("nan"), float("inf")):
                self.assert_code(
                    base_request(samples=with_point(**{key: bad})), "invalid_samples"
                )
            point = sample(1.0)
            del point[key]
            self.assert_code(
                base_request(samples=[sample(0.0), point]), "invalid_samples"
            )
        # Zero readings are allowed.
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=0.0, current=0.0, acceptance=0.0),
                    sample(1.0, voltage=0.0, current=0.0, acceptance=0.0),
                ]
            )
        )
        self.assertEqual(result["available_energy_wh"], 0.0)

    def test_harvester_rules(self) -> None:
        self.assert_code(base_request(harvester=None), "invalid_harvester_config")
        self.assert_code(base_request(harvester=[]), "invalid_harvester_config")
        self.assert_code(base_request(harvester="x"), "invalid_harvester_config")
        missing = {k: v for k, v in base_request().items() if k != "harvester"}
        self.assert_code(missing, "invalid_harvester_config")

    def test_max_input_power_rules(self) -> None:
        def with_harvester(**fields):
            harvester = {
                "max_input_power_w": 25.0,
                "efficiency_curve": [
                    curve_point(0.0, 0.8),
                    curve_point(10.0, 0.9),
                ],
            }
            harvester.update(fields)
            return harvester

        for bad in (None, 0.0, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(harvester=with_harvester(max_input_power_w=bad)),
                "invalid_harvester_config",
            )

    def test_efficiency_curve_structure_rules(self) -> None:
        def with_curve(curve):
            return {
                "max_input_power_w": 25.0,
                "efficiency_curve": curve,
            }

        self.assert_code(
            base_request(harvester=with_curve(None)), "invalid_harvester_config"
        )
        self.assert_code(
            base_request(harvester=with_curve([])), "invalid_harvester_config"
        )
        self.assert_code(
            base_request(harvester=with_curve("x")), "invalid_harvester_config"
        )
        self.assert_code(
            base_request(harvester=with_curve([curve_point(0.0, 0.8)])),
            "invalid_harvester_config",
        )
        self.assert_code(
            base_request(harvester=with_curve([None, curve_point(2.0, 0.9)])),
            "invalid_harvester_config",
        )

    def test_efficiency_curve_value_rules(self) -> None:
        def with_curve(points):
            return base_request(
                harvester={"max_input_power_w": 25.0, "efficiency_curve": points}
            )

        for bad in (None, -0.1, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_curve([curve_point(bad, 0.8), curve_point(2.0, 0.9)]),
                "invalid_harvester_config",
            )
        for bad in (None, 0.0, -0.1, 1.1, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_curve([curve_point(0.0, bad), curve_point(2.0, 0.9)]),
                "invalid_harvester_config",
            )
        # Efficiency of exactly 1 is allowed.
        result = self.service.estimate_solar_harvest(
            base_request(
                samples=[
                    sample(0.0, voltage=1.0, current=1.0),
                    sample(10.0, voltage=1.0, current=1.0),
                ],
                harvester={
                    "max_input_power_w": 25.0,
                    "efficiency_curve": [
                        curve_point(0.0, 0.9),
                        curve_point(2.0, 1.0),
                    ],
                },
            )
        )
        # 1 W interpolates midway between 0 W @ 0.9 and 2 W @ 1.0 -> 0.95.
        self.assertAlmostEqual(result["estimates"][0]["converted_power_w"], 0.95)

    def test_efficiency_curve_must_be_strictly_increasing(self) -> None:
        self.assert_code(
            base_request(
                harvester={
                    "max_input_power_w": 25.0,
                    "efficiency_curve": [
                        curve_point(2.0, 0.8),
                        curve_point(2.0, 0.9),
                    ],
                }
            ),
            "invalid_harvester_config",
        )
        self.assert_code(
            base_request(
                harvester={
                    "max_input_power_w": 25.0,
                    "efficiency_curve": [
                        curve_point(3.0, 0.8),
                        curve_point(2.0, 0.9),
                    ],
                }
            ),
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
            SOLAR_HARVEST_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["estimates"]), 2)
        for key in (
            "available_energy_wh",
            "harvested_energy_wh",
            "conversion_loss_energy_wh",
            "curtailed_energy_wh",
            "rejected_energy_wh",
            "overall_efficiency",
        ):
            self.assertIn(key, body)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(SOLAR_HARVEST_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(SOLAR_HARVEST_ROUTE, b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(SOLAR_HARVEST_ROUTE, b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_samples_is_422(self) -> None:
        payload = base_request(samples=[sample(0.0)])
        status, body = self.post(
            SOLAR_HARVEST_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")
        self.assertTrue(body["error"]["message"])

    def test_invalid_harvester_is_422(self) -> None:
        payload = base_request(
            harvester={
                "max_input_power_w": 25.0,
                "efficiency_curve": [
                    curve_point(2.0, 0.9),
                    curve_point(1.0, 0.8),
                ],
            }
        )
        status, body = self.post(
            SOLAR_HARVEST_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_harvester_config")

    def test_unknown_route_is_404(self) -> None:
        status, body = self.post("/v1/energy/solar/nope", json.dumps({}).encode())
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
