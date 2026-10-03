import json
import math
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import BATTERY_MODEL_SIMULATE_ROUTE, Handler
from powerrail.service import ApiError, Service


def model(**overrides):
    values = {
        "r0_ohm": 0.05,
        "r1_ohm": 0.02,
        "c1_f": 2000.0,
    }
    values.update(overrides)
    return values


def ocv_curve():
    return [
        {"soc": 0.0, "voltage_v": 3.0},
        {"soc": 0.5, "voltage_v": 3.6},
        {"soc": 1.0, "voltage_v": 4.2},
    ]


def base_request(**overrides):
    payload = {
        "capacity_ah": 2.0,
        "initial_soc": 0.5,
        "model": model(),
        "ocv_curve": ocv_curve(),
        "samples": [
            {"timestamp_s": 0.0, "current_a": 1.0},
            {"timestamp_s": 60.0, "current_a": 1.0},
            {"timestamp_s": 120.0, "current_a": -0.5},
        ],
    }
    payload.update(overrides)
    return payload


class BatteryModelSimulateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def simulate(self, **overrides):
        return self.service.simulate_battery_model(base_request(**overrides))

    def test_result_shape(self) -> None:
        result = self.simulate()
        self.assertEqual(
            set(result), {"estimates", "final_soc", "final_terminal_voltage_v"}
        )
        self.assertEqual(len(result["estimates"]), 3)
        for estimate in result["estimates"]:
            self.assertEqual(
                set(estimate),
                {
                    "timestamp_s",
                    "soc",
                    "ocv_voltage_v",
                    "polarization_voltage_v",
                    "terminal_voltage_v",
                },
            )

    def test_first_estimate_uses_initial_soc_and_zero_polarization(self) -> None:
        result = self.simulate()
        first = result["estimates"][0]
        self.assertEqual(first["timestamp_s"], 0.0)
        self.assertEqual(first["soc"], 0.5)
        self.assertEqual(first["polarization_voltage_v"], 0.0)
        # OCV at soc 0.5 is 3.6; terminal drops by I * r0.
        self.assertAlmostEqual(first["ocv_voltage_v"], 3.6, places=12)
        self.assertAlmostEqual(first["terminal_voltage_v"], 3.6 - 1.0 * 0.05, places=12)

    def test_coulomb_counting_and_polarization_relaxation(self) -> None:
        result = self.simulate()
        estimates = result["estimates"]
        # Interval 1: I = 1.0 A over 60 s on a 2 Ah pack.
        expected_soc = 0.5 - 1.0 * 60.0 / (2.0 * 3600.0)
        self.assertAlmostEqual(estimates[1]["soc"], expected_soc, places=12)
        decay = math.exp(-60.0 / (0.02 * 2000.0))
        expected_pol = 0.02 * 1.0 * (1.0 - decay)
        self.assertAlmostEqual(
            estimates[1]["polarization_voltage_v"], expected_pol, places=12
        )
        # Interval 2: previous sample current is still 1.0 A.
        expected_soc -= 1.0 * 60.0 / (2.0 * 3600.0)
        self.assertAlmostEqual(estimates[2]["soc"], expected_soc, places=12)
        expected_pol = expected_pol * decay + 0.02 * 1.0 * (1.0 - decay)
        self.assertAlmostEqual(
            estimates[2]["polarization_voltage_v"], expected_pol, places=12
        )

    def test_terminal_voltage_uses_current_sample(self) -> None:
        result = self.simulate()
        for estimate, sample in zip(result["estimates"], base_request()["samples"]):
            expected = (
                estimate["ocv_voltage_v"]
                - sample["current_a"] * 0.05
                - estimate["polarization_voltage_v"]
            )
            self.assertAlmostEqual(estimate["terminal_voltage_v"], expected, places=12)

    def test_charge_current_raises_soc_and_reverses_polarization(self) -> None:
        result = self.simulate(
            samples=[
                {"timestamp_s": 0.0, "current_a": -2.0},
                {"timestamp_s": 30.0, "current_a": -2.0},
            ]
        )
        second = result["estimates"][1]
        self.assertAlmostEqual(
            second["soc"], 0.5 + 2.0 * 30.0 / (2.0 * 3600.0), places=12
        )
        self.assertLess(second["polarization_voltage_v"], 0.0)
        # Charging lifts the terminal above OCV.
        self.assertGreater(second["terminal_voltage_v"], second["ocv_voltage_v"])

    def test_soc_clamps_to_unit_interval(self) -> None:
        result = self.simulate(
            initial_soc=0.999,
            samples=[
                {"timestamp_s": 0.0, "current_a": -5.0},
                {"timestamp_s": 3600.0, "current_a": 0.0},
            ],
        )
        self.assertEqual(result["estimates"][1]["soc"], 1.0)
        result = self.simulate(
            initial_soc=0.001,
            samples=[
                {"timestamp_s": 0.0, "current_a": 5.0},
                {"timestamp_s": 3600.0, "current_a": 0.0},
            ],
        )
        self.assertEqual(result["estimates"][1]["soc"], 0.0)

    def test_ocv_interpolates_and_clamps_to_endpoints(self) -> None:
        # soc 0.25 sits halfway between curve points (0.0, 3.0) and (0.5, 3.6).
        result = self.simulate(initial_soc=0.25)
        self.assertAlmostEqual(
            result["estimates"][0]["ocv_voltage_v"], 3.3, places=12
        )
        # A curve that does not cover the full SoC range clamps to endpoints.
        result = self.simulate(
            initial_soc=0.9,
            ocv_curve=[
                {"soc": 0.2, "voltage_v": 3.4},
                {"soc": 0.8, "voltage_v": 4.0},
            ],
        )
        self.assertAlmostEqual(
            result["estimates"][0]["ocv_voltage_v"], 4.0, places=12
        )

    def test_final_summaries_match_last_estimate(self) -> None:
        result = self.simulate()
        last = result["estimates"][-1]
        self.assertEqual(result["final_soc"], last["soc"])
        self.assertEqual(
            result["final_terminal_voltage_v"], last["terminal_voltage_v"]
        )

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.simulate_battery_model(payload)
        self.assertEqual(payload, snapshot)


class BatteryModelSimulateValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.simulate_battery_model(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_capacity_rules(self) -> None:
        for bad in (None, 0.0, -1.0, float("nan"), float("inf"), True, "2"):
            self.assert_code(base_request(capacity_ah=bad), "invalid_capacity")

    def test_initial_soc_rules(self) -> None:
        for bad in (None, -0.1, 1.1, float("nan"), float("inf"), True, "0.5"):
            self.assert_code(base_request(initial_soc=bad), "invalid_initial_soc")
        # Boundary values are allowed.
        for good in (0.0, 1.0):
            result = self.service.simulate_battery_model(
                base_request(initial_soc=good)
            )
            self.assertEqual(result["estimates"][0]["soc"], good)

    def test_model_rules(self) -> None:
        self.assert_code(base_request(model=None), "invalid_battery_model")
        self.assert_code(base_request(model=[1, 2]), "invalid_battery_model")
        request_without_model = {
            k: v for k, v in base_request().items() if k != "model"
        }
        self.assert_code(request_without_model, "invalid_battery_model")
        for bad in (None, 0.0, -0.1, float("nan"), float("inf"), True, "0.05"):
            self.assert_code(
                base_request(model=model(r0_ohm=bad)), "invalid_battery_model"
            )
            self.assert_code(
                base_request(model=model(r1_ohm=bad)), "invalid_battery_model"
            )
            self.assert_code(
                base_request(model=model(c1_f=bad)), "invalid_battery_model"
            )

    def test_ocv_curve_rules(self) -> None:
        self.assert_code(base_request(ocv_curve=None), "invalid_ocv_curve")
        self.assert_code(base_request(ocv_curve=[]), "invalid_ocv_curve")
        self.assert_code(
            base_request(ocv_curve=[{"soc": 0.0, "voltage_v": 3.0}]),
            "invalid_ocv_curve",
        )
        self.assert_code(
            base_request(ocv_curve=[3.0, 4.2]), "invalid_ocv_curve"
        )
        # Non-finite or out-of-range values.
        for bad_point in (
            {"soc": float("nan"), "voltage_v": 3.0},
            {"soc": 0.0, "voltage_v": float("inf")},
            {"soc": -0.1, "voltage_v": 3.0},
            {"soc": 1.1, "voltage_v": 3.0},
            {"soc": True, "voltage_v": 3.0},
            {"soc": 0.0, "voltage_v": "3.0"},
        ):
            self.assert_code(
                base_request(
                    ocv_curve=[bad_point, {"soc": 1.0, "voltage_v": 4.2}]
                ),
                "invalid_ocv_curve",
            )
        # voltage_v must be strictly increasing.
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"soc": 0.0, "voltage_v": 3.6},
                    {"soc": 1.0, "voltage_v": 3.6},
                ]
            ),
            "invalid_ocv_curve",
        )
        # soc must be strictly increasing (equal values rejected).
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"soc": 0.5, "voltage_v": 3.0},
                    {"soc": 0.5, "voltage_v": 4.2},
                ]
            ),
            "invalid_ocv_curve",
        )

    def test_samples_rules(self) -> None:
        self.assert_code(base_request(samples=None), "invalid_samples")
        self.assert_code(base_request(samples=[]), "invalid_samples")
        self.assert_code(base_request(samples="x"), "invalid_samples")
        self.assert_code(base_request(samples=[1.0]), "invalid_samples")

    def test_timestamp_rules(self) -> None:
        for bad in (None, float("nan"), float("inf"), True, "0"):
            self.assert_code(
                base_request(samples=[{"timestamp_s": bad, "current_a": 1.0}]),
                "invalid_timestamp",
            )
        # Missing key and non-increasing order.
        self.assert_code(
            base_request(samples=[{"current_a": 1.0}]), "invalid_timestamp"
        )
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 10.0, "current_a": 1.0},
                    {"timestamp_s": 10.0, "current_a": 1.0},
                ]
            ),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 10.0, "current_a": 1.0},
                    {"timestamp_s": 5.0, "current_a": 1.0},
                ]
            ),
            "invalid_timestamp",
        )

    def test_current_rules(self) -> None:
        for bad in (None, float("nan"), float("inf"), True, "1.0"):
            self.assert_code(
                base_request(samples=[{"timestamp_s": 0.0, "current_a": bad}]),
                "invalid_current",
            )
        self.assert_code(
            base_request(samples=[{"timestamp_s": 0.0}]), "invalid_current"
        )
        # Zero and negative currents are valid.
        result = self.service.simulate_battery_model(
            base_request(samples=[{"timestamp_s": 0.0, "current_a": 0.0}])
        )
        self.assertEqual(len(result["estimates"]), 1)

    def test_validation_order(self) -> None:
        # capacity is checked before initial_soc, model, curve and samples.
        self.assert_code(
            base_request(capacity_ah=0.0, initial_soc=2.0, model=None),
            "invalid_capacity",
        )
        self.assert_code(
            base_request(initial_soc=2.0, model=None), "invalid_initial_soc"
        )
        self.assert_code(
            base_request(model=None, ocv_curve=None), "invalid_battery_model"
        )
        self.assert_code(
            base_request(ocv_curve=None, samples=None), "invalid_ocv_curve"
        )
        self.assert_code(
            base_request(samples=None), "invalid_samples"
        )
        # Timestamps are checked before currents.
        self.assert_code(
            base_request(
                samples=[{"timestamp_s": float("nan"), "current_a": "x"}]
            ),
            "invalid_timestamp",
        )


class BatteryModelSimulateHttpTest(unittest.TestCase):
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

    def test_route_returns_200(self) -> None:
        status, body = self.post(
            BATTERY_MODEL_SIMULATE_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["estimates"]), 3)
        self.assertEqual(body["final_soc"], body["estimates"][-1]["soc"])

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_MODEL_SIMULATE_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_MODEL_SIMULATE_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_MODEL_SIMULATE_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_capacity_is_422(self) -> None:
        status, body = self.post(
            BATTERY_MODEL_SIMULATE_ROUTE,
            json.dumps(base_request(capacity_ah=-1)).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_capacity")

    def test_invalid_model_is_422(self) -> None:
        status, body = self.post(
            BATTERY_MODEL_SIMULATE_ROUTE,
            json.dumps(base_request(model=model(r1_ohm=0))).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_battery_model")

    def test_invalid_ocv_curve_is_422(self) -> None:
        status, body = self.post(
            BATTERY_MODEL_SIMULATE_ROUTE,
            json.dumps(base_request(ocv_curve=[])).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_ocv_curve")

    def test_invalid_timestamp_is_422(self) -> None:
        status, body = self.post(
            BATTERY_MODEL_SIMULATE_ROUTE,
            json.dumps(
                base_request(
                    samples=[
                        {"timestamp_s": 5.0, "current_a": 1.0},
                        {"timestamp_s": 5.0, "current_a": 1.0},
                    ]
                )
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_timestamp")

    def test_invalid_current_is_422(self) -> None:
        status, body = self.post(
            BATTERY_MODEL_SIMULATE_ROUTE,
            json.dumps(
                base_request(samples=[{"timestamp_s": 0.0, "current_a": True}])
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_current")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/model/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
