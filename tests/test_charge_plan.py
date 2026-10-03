import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import CHARGE_PLAN_ROUTE, Handler
from powerrail.service import ApiError, Service


def config(**overrides):
    values = {
        "max_current_a": 50.0,
        "charge_voltage_v": 400.0,
        "coulombic_efficiency": 0.95,
        "taper_start_soc": 0.5,
        "taper_end_current_a": 10.0,
    }
    values.update(overrides)
    return values


def base_request(**overrides):
    payload = {
        "capacity_ah": 100.0,
        "initial_soc": 0.2,
        "target_soc": 0.8,
        "step_duration_s": 1800.0,
        "max_steps": 100,
        "config": config(),
    }
    payload.update(overrides)
    return payload


class ChargePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def plan(self, **overrides):
        return self.service.plan_charging(base_request(**overrides))

    def test_step_shape(self) -> None:
        result = self.plan()
        self.assertEqual(
            set(result),
            {"steps", "final_soc", "elapsed_s", "input_energy_wh", "status"},
        )
        for step in result["steps"]:
            self.assertEqual(
                set(step),
                {"start_soc", "end_soc", "duration_s", "current_a", "input_energy_wh"},
            )

    def test_constant_current_below_taper_start(self) -> None:
        # Every step starts at or below taper_start_soc, so all use max current.
        result = self.plan(
            initial_soc=0.2,
            target_soc=0.49,
            config=config(taper_start_soc=0.48),
        )
        for step in result["steps"]:
            self.assertEqual(step["current_a"], 50.0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_soc"], 0.49)

    def test_taper_current_decreases_linearly(self) -> None:
        result = self.plan(initial_soc=0.5, target_soc=0.8)
        # The first step starts exactly at taper_start_soc -> max current.
        self.assertEqual(result["steps"][0]["current_a"], 50.0)
        previous = 50.0
        for step in result["steps"][1:]:
            self.assertLess(step["current_a"], previous)
            # current = max - span * (soc - taper_start) / taper_span
            expected = 50.0 - 40.0 * (step["start_soc"] - 0.5) / 0.3
            self.assertAlmostEqual(step["current_a"], expected, places=12)
            previous = step["current_a"]
        self.assertGreater(previous, 10.0)
        self.assertEqual(result["status"], "completed")

    def test_step_delta_follows_coulomb_counting(self) -> None:
        result = self.plan()
        for step in result["steps"]:
            expected_delta = (
                step["current_a"] * 0.95 * step["duration_s"] / (100.0 * 3600.0)
            )
            self.assertAlmostEqual(
                step["end_soc"] - step["start_soc"], expected_delta, places=12
            )
            expected_energy = (
                400.0 * step["current_a"] * step["duration_s"] / 3600.0
            )
            self.assertAlmostEqual(
                step["input_energy_wh"], expected_energy, places=9
            )

    def test_steps_chain_and_summaries_aggregate(self) -> None:
        result = self.plan()
        for previous, step in zip(result["steps"], result["steps"][1:]):
            self.assertEqual(previous["end_soc"], step["start_soc"])
        self.assertEqual(result["steps"][0]["start_soc"], 0.2)
        self.assertAlmostEqual(
            result["elapsed_s"],
            sum(step["duration_s"] for step in result["steps"]),
            places=9,
        )
        self.assertAlmostEqual(
            result["input_energy_wh"],
            sum(step["input_energy_wh"] for step in result["steps"]),
            places=9,
        )

    def test_final_step_shortened_to_land_on_target(self) -> None:
        result = self.plan()
        last = result["steps"][-1]
        self.assertEqual(last["end_soc"], 0.8)
        self.assertEqual(result["final_soc"], 0.8)
        self.assertLess(last["duration_s"], 1800.0)

    def test_initial_at_target_is_empty_completed_plan(self) -> None:
        result = self.plan(initial_soc=0.8, target_soc=0.8)
        self.assertEqual(
            result,
            {
                "steps": [],
                "final_soc": 0.8,
                "elapsed_s": 0.0,
                "input_energy_wh": 0.0,
                "status": "completed",
            },
        )

    def test_exhausted_steps_is_incomplete(self) -> None:
        result = self.plan(max_steps=1)
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertLess(result["final_soc"], 0.8)

    def test_taper_start_at_zero(self) -> None:
        result = self.plan(
            initial_soc=0.0,
            target_soc=0.01,
            step_duration_s=36.0,
            max_steps=10000,
            config=config(taper_start_soc=0.0),
        )
        self.assertEqual(result["steps"][0]["current_a"], 50.0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_soc"], 0.01)

    def test_taper_end_equal_to_max_current(self) -> None:
        result = self.plan(config=config(taper_end_current_a=50.0))
        self.assertTrue(all(step["current_a"] == 50.0 for step in result["steps"]))
        self.assertEqual(result["status"], "completed")

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.plan_charging(payload)
        self.assertEqual(payload, snapshot)


class ChargePlanValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.plan_charging(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_capacity_rules(self) -> None:
        for bad in (None, 0.0, -1.0, float("nan"), float("inf"), True, "100"):
            self.assert_code(base_request(capacity_ah=bad), "invalid_capacity")

    def test_soc_rules(self) -> None:
        for bad in (None, -0.1, 1.1, float("nan"), float("inf"), True, "0.2"):
            self.assert_code(base_request(initial_soc=bad), "invalid_soc_range")
            self.assert_code(base_request(target_soc=bad), "invalid_soc_range")
        self.assert_code(
            base_request(initial_soc=0.9, target_soc=0.8), "invalid_soc_range"
        )
        # equality is allowed.
        result = self.service.plan_charging(
            base_request(initial_soc=1.0, target_soc=1.0,
                         config=config(taper_start_soc=0.0))
        )
        self.assertEqual(result["status"], "completed")

    def test_config_rules(self) -> None:
        self.assert_code(base_request(config=None), "invalid_charge_config")
        good = config()
        request_without_config = {k: v for k, v in base_request().items() if k != "config"}
        self.assert_code(request_without_config, "invalid_charge_config")

        for bad in (None, 0.0, -1.0, float("nan"), float("inf"), True, "50"):
            self.assert_code(
                base_request(config=config(max_current_a=bad)),
                "invalid_charge_config",
            )
            self.assert_code(
                base_request(config=config(charge_voltage_v=bad)),
                "invalid_charge_config",
            )

        for bad in (None, 0.0, -0.1, 1.01, float("nan"), True, "0.95"):
            self.assert_code(
                base_request(config=config(coulombic_efficiency=bad)),
                "invalid_charge_config",
            )
        # efficiency == 1 is allowed
        result = self.service.plan_charging(
            base_request(config=config(coulombic_efficiency=1.0))
        )
        self.assertEqual(result["status"], "completed")

        for bad in (None, -0.1, 0.8, 0.9, float("nan"), True, "0.5"):
            self.assert_code(
                base_request(config=config(taper_start_soc=bad)),
                "invalid_charge_config",
            )

        for bad in (None, 0.0, -1.0, 50.01, float("nan"), True, "10"):
            self.assert_code(
                base_request(config=config(taper_end_current_a=bad)),
                "invalid_charge_config",
            )
        # taper end equal to max current is allowed
        result = self.service.plan_charging(
            base_request(config=config(taper_end_current_a=50.0))
        )
        self.assertEqual(result["status"], "completed")

    def test_plan_option_rules(self) -> None:
        for bad in (None, 0.0, -1.0, float("nan"), float("inf"), True, "1800"):
            self.assert_code(
                base_request(step_duration_s=bad), "invalid_plan_options"
            )
        for bad in (None, 0, -1, 1.5, True, "100"):
            self.assert_code(base_request(max_steps=bad), "invalid_plan_options")


class ChargePlanHttpTest(unittest.TestCase):
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
            CHARGE_PLAN_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["final_soc"], 0.8)
        self.assertTrue(body["steps"])

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(CHARGE_PLAN_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(CHARGE_PLAN_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(CHARGE_PLAN_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_capacity_is_422(self) -> None:
        status, body = self.post(
            CHARGE_PLAN_ROUTE, json.dumps(base_request(capacity_ah=-1)).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_capacity")

    def test_invalid_soc_range_is_422(self) -> None:
        status, body = self.post(
            CHARGE_PLAN_ROUTE,
            json.dumps(base_request(initial_soc=0.9)).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_soc_range")

    def test_invalid_config_is_422(self) -> None:
        status, body = self.post(
            CHARGE_PLAN_ROUTE,
            json.dumps(base_request(config=config(max_current_a=0))).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_charge_config")

    def test_invalid_plan_options_is_422(self) -> None:
        status, body = self.post(
            CHARGE_PLAN_ROUTE,
            json.dumps(base_request(max_steps=0)).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_plan_options")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/charge/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
