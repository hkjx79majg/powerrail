import json
import math
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import BATTERY_MODEL_ROUTE, HEALTH_ROUTE, Handler
from powerrail.service import ApiError, Service


def model(**overrides):
    values = {"r0_ohm": 0.1, "r1_ohm": 0.2, "c1_f": 100.0}
    values.update(overrides)
    return values


def ocv_curve():
    return [
        {"soc": 0.0, "voltage_v": 3.0},
        {"soc": 1.0, "voltage_v": 4.0},
    ]


def base_request(**overrides):
    payload = {
        "capacity_ah": 1.0,
        "initial_soc": 0.5,
        "model": model(),
        "ocv_curve": ocv_curve(),
        "samples": [
            {"timestamp_s": 0, "current_a": 1.0},
            {"timestamp_s": 20, "current_a": 2.0},
        ],
    }
    payload.update(overrides)
    return payload


class BatteryModelSimulationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_first_sample_uses_initial_soc_without_polarization(self) -> None:
        result = self.service.simulate_battery_model(base_request())
        first = result["estimates"][0]
        self.assertEqual(first["timestamp_s"], 0)
        self.assertEqual(first["soc"], 0.5)
        self.assertEqual(first["ocv_voltage_v"], 3.5)
        self.assertEqual(first["polarization_voltage_v"], 0.0)
        self.assertAlmostEqual(first["terminal_voltage_v"], 3.4)

    def test_interval_uses_previous_current(self) -> None:
        result = self.service.simulate_battery_model(base_request())
        second = result["estimates"][1]
        decay = math.exp(-1.0)
        expected_soc = 0.5 - 1.0 * 20.0 / 3600.0
        expected_pol = 0.2 * 1.0 * (1.0 - decay)
        self.assertAlmostEqual(second["soc"], expected_soc)
        self.assertAlmostEqual(second["polarization_voltage_v"], expected_pol)
        self.assertAlmostEqual(second["ocv_voltage_v"], 3.0 + expected_soc)
        self.assertAlmostEqual(
            second["terminal_voltage_v"],
            3.0 + expected_soc - 2.0 * 0.1 - expected_pol,
        )
        self.assertEqual(result["final_soc"], second["soc"])
        self.assertEqual(
            result["final_terminal_voltage_v"], second["terminal_voltage_v"]
        )

    def test_polarization_relaxes_and_charging_current_reverses_it(self) -> None:
        payload = base_request(
            samples=[
                {"timestamp_s": 0, "current_a": 1.0},
                {"timestamp_s": 20, "current_a": 0.0},
                {"timestamp_s": 40, "current_a": -1.0},
            ]
        )
        result = self.service.simulate_battery_model(payload)
        estimates = result["estimates"]
        decay = math.exp(-1.0)
        self.assertAlmostEqual(
            estimates[1]["polarization_voltage_v"], 0.2 * (1.0 - decay)
        )
        self.assertAlmostEqual(
            estimates[2]["polarization_voltage_v"],
            0.2 * (1.0 - decay) * decay,
        )
        # zero current at sample 1 leaves only polarization drop
        self.assertAlmostEqual(
            estimates[1]["terminal_voltage_v"],
            estimates[1]["ocv_voltage_v"] - 0.2 * (1.0 - decay),
        )
        # charging current raises the terminal voltage
        self.assertGreater(
            estimates[2]["terminal_voltage_v"], estimates[2]["ocv_voltage_v"]
        )

    def test_soc_clamps_to_unit_range(self) -> None:
        payload = base_request(
            initial_soc=0.001,
            samples=[
                {"timestamp_s": 0, "current_a": 10.0},
                {"timestamp_s": 3600, "current_a": 10.0},
            ],
        )
        result = self.service.simulate_battery_model(payload)
        self.assertEqual(result["estimates"][-1]["soc"], 0.0)
        self.assertEqual(result["final_soc"], 0.0)

        payload = base_request(
            initial_soc=0.999,
            samples=[
                {"timestamp_s": 0, "current_a": -10.0},
                {"timestamp_s": 3600, "current_a": -10.0},
            ],
        )
        result = self.service.simulate_battery_model(payload)
        self.assertEqual(result["estimates"][-1]["soc"], 1.0)

    def test_ocv_interpolates_and_clamps_to_endpoints(self) -> None:
        curve = [
            {"soc": 0.2, "voltage_v": 3.2},
            {"soc": 0.8, "voltage_v": 3.8},
        ]
        payload = base_request(
            initial_soc=0.5,
            ocv_curve=curve,
            samples=[{"timestamp_s": 0, "current_a": 0.0}],
        )
        first = self.service.simulate_battery_model(payload)["estimates"][0]
        self.assertAlmostEqual(first["ocv_voltage_v"], 3.5)

        payload = base_request(
            initial_soc=0.0,
            ocv_curve=curve,
            samples=[{"timestamp_s": 0, "current_a": 0.0}],
        )
        first = self.service.simulate_battery_model(payload)["estimates"][0]
        self.assertEqual(first["ocv_voltage_v"], 3.2)

        payload = base_request(
            initial_soc=1.0,
            ocv_curve=curve,
            samples=[{"timestamp_s": 0, "current_a": 0.0}],
        )
        first = self.service.simulate_battery_model(payload)["estimates"][0]
        self.assertEqual(first["ocv_voltage_v"], 3.8)

    def test_estimates_match_samples_in_length_and_order(self) -> None:
        payload = base_request(
            samples=[{"timestamp_s": index, "current_a": 1.0} for index in range(5)]
        )
        result = self.service.simulate_battery_model(payload)
        self.assertEqual(len(result["estimates"]), 5)
        self.assertEqual(
            [item["timestamp_s"] for item in result["estimates"]],
            [0, 1, 2, 3, 4],
        )

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.simulate_battery_model(payload)
        self.assertEqual(payload, snapshot)


class BatteryModelValidationTest(unittest.TestCase):
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
        self.assert_code(base_request(capacity_ah=None), "invalid_capacity")
        self.assert_code(base_request(capacity_ah=0), "invalid_capacity")
        self.assert_code(base_request(capacity_ah=-1.0), "invalid_capacity")
        self.assert_code(base_request(capacity_ah=True), "invalid_capacity")
        self.assert_code(base_request(capacity_ah="x"), "invalid_capacity")
        self.assert_code(base_request(capacity_ah=float("nan")), "invalid_capacity")
        self.assert_code(
            {k: v for k, v in base_request().items() if k != "capacity_ah"},
            "invalid_capacity",
        )

    def test_initial_soc_rules(self) -> None:
        self.assert_code(base_request(initial_soc=None), "invalid_initial_soc")
        self.assert_code(base_request(initial_soc=-0.1), "invalid_initial_soc")
        self.assert_code(base_request(initial_soc=1.1), "invalid_initial_soc")
        self.assert_code(base_request(initial_soc=True), "invalid_initial_soc")
        self.assert_code(
            base_request(initial_soc=float("inf")), "invalid_initial_soc"
        )

    def test_model_rules(self) -> None:
        self.assert_code(base_request(model=None), "invalid_battery_model")
        self.assert_code(base_request(model=[]), "invalid_battery_model")
        for key in ("r0_ohm", "r1_ohm", "c1_f"):
            self.assert_code(
                base_request(model=model(**{key: 0})), "invalid_battery_model"
            )
            self.assert_code(
                base_request(model=model(**{key: -1.0})), "invalid_battery_model"
            )
            self.assert_code(
                base_request(model=model(**{key: True})), "invalid_battery_model"
            )
            self.assert_code(
                base_request(model=model(**{key: float("nan")})),
                "invalid_battery_model",
            )

    def test_ocv_curve_rules(self) -> None:
        self.assert_code(base_request(ocv_curve=None), "invalid_ocv_curve")
        self.assert_code(
            base_request(ocv_curve=[{"soc": 0.0, "voltage_v": 3.0}]),
            "invalid_ocv_curve",
        )
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"soc": 1.0, "voltage_v": 3.0},
                    {"soc": 0.0, "voltage_v": 4.0},
                ]
            ),
            "invalid_ocv_curve",
        )
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"soc": 0.0, "voltage_v": 4.0},
                    {"soc": 1.0, "voltage_v": 3.0},
                ]
            ),
            "invalid_ocv_curve",
        )
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"soc": -0.1, "voltage_v": 3.0},
                    {"soc": 1.0, "voltage_v": 4.0},
                ]
            ),
            "invalid_ocv_curve",
        )
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"soc": 0.0, "voltage_v": True},
                    {"soc": 1.0, "voltage_v": 4.0},
                ]
            ),
            "invalid_ocv_curve",
        )

    def test_samples_rules(self) -> None:
        self.assert_code(base_request(samples=None), "invalid_samples")
        self.assert_code(base_request(samples=[]), "invalid_samples")
        self.assert_code(base_request(samples="x"), "invalid_samples")
        self.assert_code(base_request(samples=[1]), "invalid_samples")

    def test_timestamp_rules(self) -> None:
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 0, "current_a": 1.0},
                    {"timestamp_s": 0, "current_a": 1.0},
                ]
            ),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(samples=[{"current_a": 1.0}]),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 0, "current_a": 1.0},
                    {"timestamp_s": float("nan"), "current_a": 1.0},
                ]
            ),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": True, "current_a": 1.0},
                ]
            ),
            "invalid_timestamp",
        )

    def test_current_rules(self) -> None:
        self.assert_code(
            base_request(samples=[{"timestamp_s": 0}]),
            "invalid_current",
        )
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 0, "current_a": 1.0},
                    {"timestamp_s": 1, "current_a": True},
                ]
            ),
            "invalid_current",
        )
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 0, "current_a": 1.0},
                    {"timestamp_s": 1, "current_a": float("inf")},
                ]
            ),
            "invalid_current",
        )
        # zero and negative currents are legal (rest / charging)
        result = self.service.simulate_battery_model(
            base_request(
                samples=[
                    {"timestamp_s": 0, "current_a": 0.0},
                    {"timestamp_s": 1, "current_a": -1.0},
                ]
            )
        )
        self.assertEqual(len(result["estimates"]), 2)

    def test_validation_ordering(self) -> None:
        # invalid model is reported before invalid ocv curve and samples
        self.assert_code(
            base_request(model=model(r0_ohm=0), ocv_curve=[], samples=[]),
            "invalid_battery_model",
        )
        # invalid ocv curve is reported before invalid samples
        self.assert_code(base_request(ocv_curve=[], samples=[]), "invalid_ocv_curve")
        # samples structure is reported before per-field errors
        self.assert_code(base_request(samples=[{}]), "invalid_timestamp")
        # timestamps are validated before currents
        self.assert_code(
            base_request(
                samples=[
                    {"timestamp_s": 0, "current_a": 1.0},
                    {"timestamp_s": 0, "current_a": "bad"},
                ]
            ),
            "invalid_timestamp",
        )


class BatteryModelHttpTest(unittest.TestCase):
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

    def test_simulate_route_returns_200(self) -> None:
        status, body = self.post(
            BATTERY_MODEL_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["estimates"]), 2)
        self.assertAlmostEqual(body["final_soc"], 0.5 - 20.0 / 3600.0)
        self.assertAlmostEqual(body["final_terminal_voltage_v"], body["estimates"][-1]["terminal_voltage_v"])

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_MODEL_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_MODEL_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(BATTERY_MODEL_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_current_is_422(self) -> None:
        payload = base_request(
            samples=[{"timestamp_s": 0, "current_a": "nope"}]
        )
        status, body = self.post(
            BATTERY_MODEL_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_current")

    def test_existing_routes_unchanged(self) -> None:
        status, body = self.post(HEALTH_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")
        with urllib.request.urlopen(self.base + "/healthz") as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
