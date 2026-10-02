import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from powerrail.server import Handler
from powerrail.service import Service, SocError


def base_request(**overrides):
    payload = {
        "capacity_ah": 100.0,
        "initial_soc": 0.8,
        "samples": [
            {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.7},
            {"timestamp_s": 3600, "current_a": 50.0, "voltage_v": 3.6},
        ],
    }
    payload.update(overrides)
    return payload


class CoulombCountingTest(unittest.TestCase):
    def test_first_item_uses_initial_soc(self) -> None:
        result = Service().estimate_soc(base_request())
        estimates = result["estimates"]
        self.assertEqual(len(estimates), 2)
        self.assertEqual(estimates[0], {"timestamp_s": 0, "soc": 0.8, "source": "coulomb"})
        self.assertEqual(result["final_soc"], estimates[-1]["soc"])

    def test_discharge_decreases_soc_with_average_current(self) -> None:
        result = Service().estimate_soc(base_request())
        # avg current (0 + 50)/2 = 25 A over 3600 s; capacity = 360000 As
        # soc = 0.8 - 25*3600/360000 = 0.55
        self.assertAlmostEqual(result["estimates"][1]["soc"], 0.55)
        self.assertEqual(result["estimates"][1]["source"], "coulomb")
        self.assertAlmostEqual(result["final_soc"], 0.55)

    def test_negative_current_charges(self) -> None:
        payload = base_request(samples=[
            {"timestamp_s": 0.0, "current_a": 0.0, "voltage_v": 3.7},
            {"timestamp_s": 1800.0, "current_a": -20.0, "voltage_v": 3.8},
        ])
        result = Service().estimate_soc(payload)
        # avg = -10 A: soc = 0.8 - (-10)*1800/360000 = 0.85
        self.assertAlmostEqual(result["estimates"][1]["soc"], 0.85)

    def test_soc_is_clamped_to_unit_interval(self) -> None:
        payload = base_request(samples=[
            {"timestamp_s": 0.0, "current_a": 0.0, "voltage_v": 3.0},
            {"timestamp_s": 1.0, "current_a": 1000000.0, "voltage_v": 2.5},
        ])
        result = Service().estimate_soc(payload)
        self.assertEqual(result["estimates"][1]["soc"], 0.0)

        payload = base_request(initial_soc=1.0, samples=[
            {"timestamp_s": 0.0, "current_a": 0.0, "voltage_v": 4.2},
            {"timestamp_s": 1.0, "current_a": -1000000.0, "voltage_v": 4.3},
        ])
        result = Service().estimate_soc(payload)
        self.assertEqual(result["estimates"][1]["soc"], 1.0)

    def test_estimates_match_input_length_and_order(self) -> None:
        samples = [{"timestamp_s": i, "current_a": 1.0, "voltage_v": 3.7} for i in range(5)]
        result = Service().estimate_soc(base_request(samples=samples))
        self.assertEqual([e["timestamp_s"] for e in result["estimates"]], list(range(5)))


class OcvCorrectionTest(unittest.TestCase):
    CURVE = [
        {"voltage_v": 3.0, "soc": 0.0},
        {"voltage_v": 4.0, "soc": 1.0},
    ]

    def test_rest_accumulates_and_corrects_via_interpolation(self) -> None:
        # Both endpoints below default 0.05 A threshold; two 200 s gaps reach 400 >= 300 s.
        payload = base_request(
            ocv_curve=self.CURVE,
            samples=[
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.5},
                {"timestamp_s": 200, "current_a": 0.0, "voltage_v": 3.5},
                {"timestamp_s": 400, "current_a": 0.0, "voltage_v": 3.5},
            ],
        )
        result = Service().estimate_soc(payload)
        sources = [e["source"] for e in result["estimates"]]
        self.assertEqual(sources, ["coulomb", "coulomb", "ocv_corrected"])
        # coulomb soc stays 0.8; ocv_soc for 3.5 V = 0.5
        self.assertAlmostEqual(result["estimates"][2]["soc"], 0.8 * 0.8 + 0.5 * 0.2)

    def test_non_rest_current_resets_accumulated_time(self) -> None:
        payload = base_request(
            ocv_curve=self.CURVE,
            samples=[
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.5},
                {"timestamp_s": 200, "current_a": 0.0, "voltage_v": 3.5},
                {"timestamp_s": 400, "current_a": 1.0, "voltage_v": 3.5},
                {"timestamp_s": 600, "current_a": 0.0, "voltage_v": 3.5},
            ],
        )
        result = Service().estimate_soc(payload)
        # rest resets at t=400; only 200 s accumulated by t=600
        self.assertTrue(all(e["source"] == "coulomb" for e in result["estimates"]))

    def test_threshold_applies_to_both_interval_endpoints(self) -> None:
        payload = base_request(
            ocv_curve=self.CURVE,
            samples=[
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 3.5},
                {"timestamp_s": 200, "current_a": 0.0, "voltage_v": 3.5},
                {"timestamp_s": 500, "current_a": 0.0, "voltage_v": 3.5},
            ],
        )
        result = Service().estimate_soc(payload)
        # first gap has |1.0| at the left end -> no accumulation; second gap alone is 300 s
        self.assertEqual(
            [e["source"] for e in result["estimates"]],
            ["coulomb", "coulomb", "ocv_corrected"],
        )

    def test_out_of_range_voltage_uses_nearest_endpoint(self) -> None:
        payload = base_request(
            ocv_curve=self.CURVE,
            rest_duration_s=100,
            samples=[
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 2.0},
                {"timestamp_s": 100, "current_a": 0.0, "voltage_v": 5.0},
                {"timestamp_s": 200, "current_a": 0.0, "voltage_v": 2.5},
            ],
        )
        result = Service().estimate_soc(payload)
        # t=100: correction pulls soc toward the top endpoint (1.0)
        self.assertAlmostEqual(result["estimates"][1]["soc"], 0.8 * 0.8 + 1.0 * 0.2)
        # t=200: zero current keeps coulomb soc at 0.84; correction pulls toward bottom (0.0)
        self.assertAlmostEqual(result["estimates"][2]["soc"], 0.84 * 0.8 + 0.0 * 0.2)

    def test_custom_options(self) -> None:
        payload = base_request(
            ocv_curve=self.CURVE,
            rest_current_a=1.0,
            rest_duration_s=100,
            ocv_weight=0.5,
            samples=[
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.5},
                {"timestamp_s": 100, "current_a": 0.5, "voltage_v": 3.5},
            ],
        )
        result = Service().estimate_soc(payload)
        self.assertEqual(result["estimates"][1]["source"], "ocv_corrected")
        # avg current 0.25 A over 100 s nudges coulomb soc slightly below 0.8 first
        coulomb = 0.8 - 0.25 * 100 / 360000.0
        self.assertAlmostEqual(result["estimates"][1]["soc"], 0.5 * coulomb + 0.5 * 0.5)


class ValidationTest(unittest.TestCase):
    def assert_code(self, payload, code, status=422):
        with self.assertRaises(SocError) as ctx:
            Service().estimate_soc(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertIn("error", ctx.exception.payload())
        self.assertEqual(ctx.exception.payload()["error"]["code"], code)

    def test_non_object_body_is_invalid_json(self) -> None:
        for body in ([], "x", 5, None):
            with self.assertRaises(SocError) as ctx:
                Service().estimate_soc(body)
            self.assertEqual(ctx.exception.code, "invalid_json")
            self.assertEqual(ctx.exception.status, 400)

    def test_capacity_failures(self) -> None:
        for value in (None, 0, -1.0, float("inf"), float("nan"), "5"):
            self.assert_code(base_request(capacity_ah=value), "invalid_capacity")
        self.assert_code({k: v for k, v in base_request().items() if k != "capacity_ah"},
                         "invalid_capacity")

    def test_initial_soc_failures(self) -> None:
        for value in (None, -0.1, 1.1, float("inf"), float("nan"), "0.5", True):
            self.assert_code(base_request(initial_soc=value), "invalid_initial_soc")
        self.assert_code({k: v for k, v in base_request().items() if k != "initial_soc"},
                         "invalid_initial_soc")

    def test_samples_failures(self) -> None:
        for value in (None, [], {}, "x", 1):
            self.assert_code(base_request(samples=value), "invalid_samples")
        self.assert_code({k: v for k, v in base_request().items() if k != "samples"},
                         "invalid_samples")

    def test_timestamp_failures(self) -> None:
        good = {"timestamp_s": 1, "current_a": 0.0, "voltage_v": 3.7}
        self.assert_code(base_request(samples=[{"current_a": 0.0, "voltage_v": 3.7}, good]),
                         "invalid_timestamp")
        self.assert_code(base_request(samples=[
            {"timestamp_s": "0", "current_a": 0.0, "voltage_v": 3.7}, good]), "invalid_timestamp")
        self.assert_code(base_request(samples=[
            {"timestamp_s": 1.0, "current_a": 0.0, "voltage_v": 3.7},
            {"timestamp_s": 1.0, "current_a": 0.0, "voltage_v": 3.7}]), "invalid_timestamp")
        self.assert_code(base_request(samples=[
            {"timestamp_s": 2.0, "current_a": 0.0, "voltage_v": 3.7},
            {"timestamp_s": 1.0, "current_a": 0.0, "voltage_v": 3.7}]), "invalid_timestamp")
        self.assert_code(base_request(samples=[[1, 0, 3.7]]), "invalid_timestamp")

    def test_measurement_failures(self) -> None:
        for field, bad in (("current_a", None), ("current_a", "1"), ("current_a", float("nan")),
                           ("voltage_v", None), ("voltage_v", []), ("voltage_v", float("inf"))):
            samples = [
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.7},
                {"timestamp_s": 1, "current_a": 0.0, "voltage_v": 3.7},
            ]
            samples[1][field] = bad
            self.assert_code(base_request(samples=samples), "invalid_measurement")
        samples = [{"timestamp_s": 0, "voltage_v": 3.7},
                   {"timestamp_s": 1, "current_a": 0.0, "voltage_v": 3.7}]
        self.assert_code(base_request(samples=samples), "invalid_measurement")

    def test_ocv_curve_failures(self) -> None:
        good_curve = [{"voltage_v": 3.0, "soc": 0.0}, {"voltage_v": 4.0, "soc": 1.0}]
        bad_curves = [
            [{"voltage_v": 3.0, "soc": 0.0}],
            [{"voltage_v": 3.0, "soc": 0.0}, {"voltage_v": 4.0}],
            [{"voltage_v": 3.0, "soc": 0.0}, {"voltage_v": 4.0, "soc": 1.5}],
            [{"voltage_v": 3.0, "soc": 0.0}, {"voltage_v": 4.0, "soc": -0.1}],
            [{"voltage_v": 4.0, "soc": 0.0}, {"voltage_v": 3.0, "soc": 1.0}],
            [{"voltage_v": 3.0, "soc": 0.0}, {"voltage_v": 3.0, "soc": 1.0}],
            [{"voltage_v": 3.0, "soc": 0.8}, {"voltage_v": 4.0, "soc": 0.2}],
            [{"voltage_v": "3.0", "soc": 0.0}, {"voltage_v": 4.0, "soc": 1.0}],
            "x",
        ]
        for curve in bad_curves:
            self.assert_code(base_request(ocv_curve=curve), "invalid_ocv_curve")
        # a valid curve does not raise
        Service().estimate_soc(base_request(ocv_curve=good_curve))

    def test_options_failures(self) -> None:
        for key, bad in (("rest_current_a", -0.01), ("rest_current_a", "x"),
                         ("rest_duration_s", -1), ("rest_duration_s", float("nan")),
                         ("ocv_weight", -0.1), ("ocv_weight", 1.1), ("ocv_weight", None)):
            self.assert_code(base_request(**{key: bad}), "invalid_options")
        # zero threshold/duration and boundary weights are allowed
        Service().estimate_soc(base_request(rest_current_a=0.0, rest_duration_s=0.0, ocv_weight=0.0))
        Service().estimate_soc(base_request(ocv_weight=1.0))


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def post(self, raw_body: bytes | None, path: str = "/v1/battery/soc/estimate"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if raw_body is not None:
            conn.request("POST", path, body=raw_body, headers=headers)
        else:
            conn.request("POST", path, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_success_endpoint(self) -> None:
        status, payload = self.post(json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["estimates"]), 2)
        self.assertAlmostEqual(payload["final_soc"], 0.55)

    def test_invalid_json(self) -> None:
        for body in (b"{not json", b"[1,2]", b'"str"', b""):
            status, payload = self.post(body)
            self.assertEqual(status, 400, body)
            self.assertEqual(payload["error"]["code"], "invalid_json")

    def test_validation_status_and_error_object(self) -> None:
        status, payload = self.post(json.dumps(base_request(capacity_ah=0)).encode())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "invalid_capacity")

    def test_unknown_post_path_still_404(self) -> None:
        status, payload = self.post(b"{}", path="/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_healthz_unchanged(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        payload = json.loads(resp.read().decode())
        self.assertEqual(payload["status"], "ok")
        conn.request("GET", "/missing")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        self.assertEqual(json.loads(resp.read().decode())["error"]["code"], "not_found")
        conn.close()


if __name__ == "__main__":
    unittest.main()
