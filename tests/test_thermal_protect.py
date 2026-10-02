import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import THERMAL_PROTECT_ROUTE, Handler
from powerrail.service import ApiError, Service


def sample(timestamp, temperature, current=2.0):
    return {
        "timestamp_s": timestamp,
        "temperature_c": temperature,
        "requested_current_a": current,
    }


def base_request(**overrides):
    payload = {
        "protection": {
            "max_charge_current_a": 10.0,
            "recovery_temperature_c": 30.0,
            "warning_temperature_c": 40.0,
            "critical_temperature_c": 50.0,
        },
        "samples": [
            sample(0.0, 25.0),
            sample(1.0, 45.0),
        ],
    }
    payload.update(overrides)
    return payload


class ThermalProtectDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_decisions_match_samples_in_order(self) -> None:
        result = self.service.protect_thermal(base_request())
        self.assertEqual(len(result["decisions"]), 2)
        self.assertEqual(
            [d["timestamp_s"] for d in result["decisions"]], [0.0, 1.0]
        )
        for item in result["decisions"]:
            self.assertEqual(
                set(item),
                {"timestamp_s", "allowed_current_a", "thermal_limit_a", "state"},
            )

    def test_normal_zone_allows_up_to_max(self) -> None:
        result = self.service.protect_thermal(
            base_request(samples=[sample(0.0, 39.9, current=20.0)])
        )
        item = result["decisions"][0]
        self.assertEqual(item["state"], "normal")
        self.assertEqual(item["thermal_limit_a"], 10.0)
        self.assertEqual(item["allowed_current_a"], 10.0)
        self.assertEqual(result["final_state"], "normal")
        self.assertEqual(result["cutoff_count"], 0)

    def test_requested_current_below_limit_passes_through(self) -> None:
        result = self.service.protect_thermal(
            base_request(samples=[sample(0.0, 25.0, current=3.0)])
        )
        item = result["decisions"][0]
        self.assertEqual(item["thermal_limit_a"], 10.0)
        self.assertEqual(item["allowed_current_a"], 3.0)

    def test_derated_zone_ramps_linearly_to_zero(self) -> None:
        result = self.service.protect_thermal(
            base_request(
                samples=[
                    sample(0.0, 40.0),
                    sample(1.0, 45.0),
                    sample(2.0, 49.0),
                ]
            )
        )
        limits = [d["thermal_limit_a"] for d in result["decisions"]]
        self.assertAlmostEqual(limits[0], 10.0)
        self.assertAlmostEqual(limits[1], 5.0)
        self.assertAlmostEqual(limits[2], 1.0)
        for item in result["decisions"]:
            self.assertEqual(item["state"], "derated")
        self.assertAlmostEqual(result["decisions"][1]["allowed_current_a"], 2.0)

    def test_critical_temperature_latches_cutoff(self) -> None:
        result = self.service.protect_thermal(
            base_request(
                samples=[
                    sample(0.0, 50.0),
                    sample(1.0, 45.0),
                    sample(2.0, 35.0),
                ]
            )
        )
        states = [d["state"] for d in result["decisions"]]
        self.assertEqual(states, ["cutoff", "cutoff", "cutoff"])
        for item in result["decisions"]:
            self.assertEqual(item["thermal_limit_a"], 0.0)
            self.assertEqual(item["allowed_current_a"], 0.0)
        self.assertEqual(result["final_state"], "cutoff")
        self.assertEqual(result["cutoff_count"], 1)

    def test_latch_releases_at_recovery_temperature(self) -> None:
        result = self.service.protect_thermal(
            base_request(
                samples=[
                    sample(0.0, 55.0),
                    sample(1.0, 30.0),
                    sample(2.0, 45.0),
                ]
            )
        )
        states = [d["state"] for d in result["decisions"]]
        self.assertEqual(states, ["cutoff", "normal", "derated"])
        self.assertAlmostEqual(result["decisions"][1]["thermal_limit_a"], 10.0)
        self.assertAlmostEqual(result["decisions"][2]["thermal_limit_a"], 5.0)
        self.assertEqual(result["final_state"], "derated")
        self.assertEqual(result["cutoff_count"], 1)

    def test_cutoff_count_tracks_each_entry(self) -> None:
        result = self.service.protect_thermal(
            base_request(
                samples=[
                    sample(0.0, 50.0),
                    sample(1.0, 30.0),
                    sample(2.0, 60.0),
                    sample(3.0, 25.0),
                ]
            )
        )
        states = [d["state"] for d in result["decisions"]]
        self.assertEqual(states, ["cutoff", "normal", "cutoff", "normal"])
        self.assertEqual(result["cutoff_count"], 2)
        self.assertEqual(result["final_state"], "normal")

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.protect_thermal(payload)
        self.assertEqual(payload, snapshot)


class ThermalProtectValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.protect_thermal(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5, 1.5, True):
            self.assert_code(payload, "invalid_json", status=400)

    def test_protection_structure_rules(self) -> None:
        self.assert_code(base_request(protection=None), "invalid_protection_config")
        self.assert_code(base_request(protection=[]), "invalid_protection_config")
        self.assert_code(base_request(protection="x"), "invalid_protection_config")
        missing = {k: v for k, v in base_request().items() if k != "protection"}
        self.assert_code(missing, "invalid_protection_config")

    def test_max_charge_current_rules(self) -> None:
        def with_protection(**fields):
            protection = base_request()["protection"]
            protection.update(fields)
            return base_request(protection=protection)

        for bad in (None, 0.0, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_protection(max_charge_current_a=bad),
                "invalid_protection_config",
            )
        protection = base_request()["protection"]
        del protection["max_charge_current_a"]
        self.assert_code(
            base_request(protection=protection), "invalid_protection_config"
        )

    def test_temperature_threshold_rules(self) -> None:
        def with_protection(**fields):
            protection = base_request()["protection"]
            protection.update(fields)
            return base_request(protection=protection)

        for key in (
            "recovery_temperature_c",
            "warning_temperature_c",
            "critical_temperature_c",
        ):
            for bad in (None, True, "x", float("nan"), float("inf")):
                self.assert_code(
                    with_protection(**{key: bad}), "invalid_protection_config"
                )
            protection = base_request()["protection"]
            del protection[key]
            self.assert_code(
                base_request(protection=protection), "invalid_protection_config"
            )

    def test_threshold_ordering_rules(self) -> None:
        def with_protection(**fields):
            protection = base_request()["protection"]
            protection.update(fields)
            return base_request(protection=protection)

        self.assert_code(
            with_protection(recovery_temperature_c=40.0),
            "invalid_protection_config",
        )
        self.assert_code(
            with_protection(warning_temperature_c=50.0),
            "invalid_protection_config",
        )
        self.assert_code(
            with_protection(critical_temperature_c=40.0),
            "invalid_protection_config",
        )
        self.assert_code(
            with_protection(
                recovery_temperature_c=50.0,
                warning_temperature_c=40.0,
                critical_temperature_c=30.0,
            ),
            "invalid_protection_config",
        )

    def test_samples_structure_rules(self) -> None:
        self.assert_code(base_request(samples=[]), "invalid_samples")
        self.assert_code(base_request(samples=None), "invalid_samples")
        self.assert_code(base_request(samples="x"), "invalid_samples")
        self.assert_code(base_request(samples=[1]), "invalid_samples")
        self.assert_code(base_request(samples=[None]), "invalid_samples")
        missing = {k: v for k, v in base_request().items() if k != "samples"}
        self.assert_code(missing, "invalid_samples")

    def test_timestamp_rules(self) -> None:
        def with_sample(**fields):
            item = sample(0.0, 25.0)
            item.update(fields)
            return base_request(samples=[item])

        for bad in (None, True, "x", float("nan"), float("inf")):
            self.assert_code(with_sample(timestamp_s=bad), "invalid_timestamp")
        item = sample(0.0, 25.0)
        del item["timestamp_s"]
        self.assert_code(base_request(samples=[item]), "invalid_timestamp")
        self.assert_code(
            base_request(samples=[sample(1.0, 25.0), sample(1.0, 26.0)]),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(samples=[sample(2.0, 25.0), sample(1.0, 26.0)]),
            "invalid_timestamp",
        )

    def test_temperature_rules(self) -> None:
        def with_sample(**fields):
            item = sample(0.0, 25.0)
            item.update(fields)
            return base_request(samples=[item])

        for bad in (None, True, "x", float("nan"), float("inf")):
            self.assert_code(with_sample(temperature_c=bad), "invalid_temperature")
        item = sample(0.0, 25.0)
        del item["temperature_c"]
        self.assert_code(base_request(samples=[item]), "invalid_temperature")

    def test_requested_current_rules(self) -> None:
        def with_sample(**fields):
            item = sample(0.0, 25.0)
            item.update(fields)
            return base_request(samples=[item])

        for bad in (None, -0.1, True, "x", float("nan"), float("inf")):
            self.assert_code(with_sample(requested_current_a=bad), "invalid_current")
        item = sample(0.0, 25.0)
        del item["requested_current_a"]
        self.assert_code(base_request(samples=[item]), "invalid_current")
        # Zero requested current is allowed.
        result = self.service.protect_thermal(with_sample(requested_current_a=0.0))
        self.assertEqual(result["decisions"][0]["allowed_current_a"], 0.0)


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
        self.assertEqual(len(body["decisions"]), 2)
        self.assertIn("final_state", body)
        self.assertIn("cutoff_count", body)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(THERMAL_PROTECT_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(THERMAL_PROTECT_ROUTE, b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(THERMAL_PROTECT_ROUTE, b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_protection_is_422(self) -> None:
        payload = base_request(protection={"max_charge_current_a": -1.0})
        status, body = self.post(THERMAL_PROTECT_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_protection_config")
        self.assertTrue(body["error"]["message"])

    def test_invalid_temperature_is_422(self) -> None:
        payload = base_request(samples=[sample(0.0, float("nan"))])
        status, body = self.post(
            THERMAL_PROTECT_ROUTE,
            json.dumps(payload).replace("NaN", "null").encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_temperature")


if __name__ == "__main__":
    unittest.main()
