import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import THERMAL_PROTECT_ROUTE, Handler
from powerrail.service import ApiError, Service


def protection(**overrides):
    config = {
        "max_charge_current_a": 10.0,
        "recovery_temperature_c": 40.0,
        "warning_temperature_c": 45.0,
        "critical_temperature_c": 60.0,
    }
    config.update(overrides)
    return config


def sample(timestamp, temperature, requested=20.0):
    return {
        "timestamp_s": timestamp,
        "temperature_c": temperature,
        "requested_current_a": requested,
    }


def base_request(**overrides):
    payload = {
        "protection": protection(),
        "samples": [
            sample(0, 30.0),
            sample(1, 50.0),
            sample(2, 60.0),
            sample(3, 55.0),
            sample(4, 40.0),
            sample(5, 50.0),
        ],
    }
    payload.update(overrides)
    return payload


class ThermalProtectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_response_shape_and_zone_transitions(self) -> None:
        result = self.service.protect_thermal(base_request())
        decisions = result["decisions"]
        self.assertEqual(len(decisions), 6)
        self.assertEqual(
            [item["timestamp_s"] for item in decisions], [0, 1, 2, 3, 4, 5]
        )
        for item in decisions:
            self.assertEqual(
                set(item),
                {"timestamp_s", "allowed_current_a", "thermal_limit_a", "state"},
            )
        self.assertEqual(
            [item["state"] for item in decisions],
            ["normal", "derated", "cutoff", "cutoff", "normal", "derated"],
        )

    def test_normal_zone_allows_full_current(self) -> None:
        result = self.service.protect_thermal(
            {"protection": protection(), "samples": [sample(0, 30.0, 8.0)]}
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["thermal_limit_a"], 10.0)
        self.assertEqual(decision["allowed_current_a"], 8.0)
        self.assertEqual(result["final_state"], "normal")
        self.assertEqual(result["cutoff_count"], 0)

    def test_derated_zone_scales_linearly_to_zero(self) -> None:
        result = self.service.protect_thermal(
            {
                "protection": protection(),
                "samples": [sample(0, 45.0), sample(1, 52.5), sample(2, 59.0)],
            }
        )
        limits = [item["thermal_limit_a"] for item in result["decisions"]]
        # limit = max * (critical - t) / (critical - warning)
        self.assertAlmostEqual(limits[0], 10.0)
        self.assertAlmostEqual(limits[1], 5.0)
        self.assertAlmostEqual(limits[2], 10.0 / 15.0)
        self.assertEqual(
            [item["state"] for item in result["decisions"]],
            ["derated", "derated", "derated"],
        )

    def test_cutoff_latches_until_recovery_threshold(self) -> None:
        result = self.service.protect_thermal(base_request())
        decisions = result["decisions"]
        # Critical temperature cuts off and latches.
        self.assertEqual(decisions[2]["thermal_limit_a"], 0.0)
        self.assertEqual(decisions[2]["allowed_current_a"], 0.0)
        # Still above recovery: stays cut off even though it left critical.
        self.assertEqual(decisions[3]["state"], "cutoff")
        self.assertEqual(decisions[3]["thermal_limit_a"], 0.0)
        # At the recovery threshold the latch releases and zones apply again.
        self.assertEqual(decisions[4]["state"], "normal")
        self.assertEqual(decisions[4]["thermal_limit_a"], 10.0)
        self.assertEqual(result["final_state"], "derated")
        self.assertEqual(result["cutoff_count"], 1)

    def test_cutoff_count_tracks_each_latch_entry(self) -> None:
        payload = {
            "protection": protection(),
            "samples": [
                sample(0, 30.0),
                sample(1, 60.0),
                sample(2, 40.0),
                sample(3, 61.0),
                sample(4, 39.0),
            ],
        }
        result = self.service.protect_thermal(payload)
        self.assertEqual(result["cutoff_count"], 2)
        self.assertEqual(result["final_state"], "normal")

    def test_allowed_current_is_min_of_request_and_limit(self) -> None:
        payload = {
            "protection": protection(),
            "samples": [sample(0, 50.0, 3.0), sample(1, 50.0, 30.0)],
        }
        result = self.service.protect_thermal(payload)
        allowed = [item["allowed_current_a"] for item in result["decisions"]]
        self.assertAlmostEqual(allowed[0], 3.0)
        self.assertAlmostEqual(allowed[1], 10.0 * 10.0 / 15.0)

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.protect_thermal(payload)
        self.assertEqual(payload, snapshot)

    def test_timestamp_is_preserved_verbatim(self) -> None:
        payload = {"protection": protection(), "samples": [sample(7, 30.0)]}
        result = self.service.protect_thermal(payload)
        self.assertEqual(result["decisions"][0]["timestamp_s"], 7)
        self.assertIsInstance(result["decisions"][0]["timestamp_s"], int)


class ThermalProtectValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.protect_thermal(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_protection_rules(self) -> None:
        good_samples = [sample(0, 30.0)]
        self.assert_code({"samples": good_samples}, "invalid_protection_config")
        self.assert_code(
            {"protection": None, "samples": good_samples}, "invalid_protection_config"
        )
        self.assert_code(
            {"protection": "x", "samples": good_samples}, "invalid_protection_config"
        )
        for bad_current in (None, 0.0, -1.0, float("nan"), float("inf"), True, "10"):
            self.assert_code(
                {
                    "protection": protection(max_charge_current_a=bad_current),
                    "samples": good_samples,
                },
                "invalid_protection_config",
            )
        for field in (
            "recovery_temperature_c",
            "warning_temperature_c",
            "critical_temperature_c",
        ):
            for bad_temp in (None, float("nan"), float("inf"), False, "45"):
                self.assert_code(
                    {
                        "protection": protection(**{field: bad_temp}),
                        "samples": good_samples,
                    },
                    "invalid_protection_config",
                )

    def test_threshold_ordering_rules(self) -> None:
        good_samples = [sample(0, 30.0)]
        # recovery must be below warning
        self.assert_code(
            {
                "protection": protection(recovery_temperature_c=45.0),
                "samples": good_samples,
            },
            "invalid_protection_config",
        )
        # warning must be below critical
        self.assert_code(
            {
                "protection": protection(warning_temperature_c=60.0),
                "samples": good_samples,
            },
            "invalid_protection_config",
        )
        self.assert_code(
            {
                "protection": protection(critical_temperature_c=40.0),
                "samples": good_samples,
            },
            "invalid_protection_config",
        )

    def test_samples_rules(self) -> None:
        self.assert_code({"protection": protection()}, "invalid_samples")
        self.assert_code(
            {"protection": protection(), "samples": []}, "invalid_samples"
        )
        self.assert_code(
            {"protection": protection(), "samples": None}, "invalid_samples"
        )
        self.assert_code(
            {"protection": protection(), "samples": "x"}, "invalid_samples"
        )
        self.assert_code(
            {"protection": protection(), "samples": [1]}, "invalid_samples"
        )

    def test_timestamp_rules(self) -> None:
        self.assert_code(
            {
                "protection": protection(),
                "samples": [{"temperature_c": 30.0, "requested_current_a": 1.0}],
            },
            "invalid_timestamp",
        )
        for bad_timestamp in (float("nan"), float("inf"), True, "1"):
            self.assert_code(
                {
                    "protection": protection(),
                    "samples": [sample(bad_timestamp, 30.0)],
                },
                "invalid_timestamp",
            )
        self.assert_code(
            {
                "protection": protection(),
                "samples": [sample(1, 30.0), sample(1, 31.0)],
            },
            "invalid_timestamp",
        )
        self.assert_code(
            {
                "protection": protection(),
                "samples": [sample(2, 30.0), sample(1, 31.0)],
            },
            "invalid_timestamp",
        )

    def test_temperature_rules(self) -> None:
        for bad_temperature in (None, float("nan"), float("inf"), False, "30"):
            self.assert_code(
                {
                    "protection": protection(),
                    "samples": [sample(0, bad_temperature)],
                },
                "invalid_temperature",
            )

    def test_current_rules(self) -> None:
        for bad_current in (None, -0.5, float("nan"), float("inf"), True, "5"):
            self.assert_code(
                {
                    "protection": protection(),
                    "samples": [sample(0, 30.0, bad_current)],
                },
                "invalid_current",
            )


class ThermalProtectHttpTest(unittest.TestCase):
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
            THERMAL_PROTECT_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["decisions"]), 6)
        self.assertEqual(body["final_state"], "derated")
        self.assertEqual(body["cutoff_count"], 1)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(THERMAL_PROTECT_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(THERMAL_PROTECT_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(THERMAL_PROTECT_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_protection_is_422(self) -> None:
        status, body = self.post(
            THERMAL_PROTECT_ROUTE,
            json.dumps({"protection": {}, "samples": [sample(0, 30.0)]}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_protection_config")

    def test_invalid_samples_is_422(self) -> None:
        status, body = self.post(
            THERMAL_PROTECT_ROUTE,
            json.dumps({"protection": protection(), "samples": []}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_invalid_timestamp_is_422(self) -> None:
        status, body = self.post(
            THERMAL_PROTECT_ROUTE,
            json.dumps(
                {
                    "protection": protection(),
                    "samples": [sample(1, 30.0), sample(1, 31.0)],
                }
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_timestamp")

    def test_invalid_temperature_is_422(self) -> None:
        status, body = self.post(
            THERMAL_PROTECT_ROUTE,
            json.dumps(
                {"protection": protection(), "samples": [sample(0, "hot")]}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_temperature")

    def test_invalid_current_is_422(self) -> None:
        status, body = self.post(
            THERMAL_PROTECT_ROUTE,
            json.dumps(
                {"protection": protection(), "samples": [sample(0, 30.0, -1.0)]}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_current")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/thermal/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
