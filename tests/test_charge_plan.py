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
        "max_current_a": 10.0,
        "charge_voltage_v": 40.0,
        "coulombic_efficiency": 1.0,
        "taper_start_soc": 0.5,
        "taper_end_current_a": 2.0,
    }
    values.update(overrides)
    return values


def base_request(**overrides):
    payload = {
        "capacity_ah": 100.0,
        "initial_soc": 0.0,
        "target_soc": 1.0,
        "step_duration_s": 3600.0,
        "max_steps": 20,
        "config": config(),
    }
    payload.update(overrides)
    return payload


class ChargePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_response_shape(self) -> None:
        result = self.service.plan_charging(base_request())
        self.assertEqual(
            set(result),
            {"steps", "final_soc", "elapsed_s", "input_energy_wh", "status"},
        )
        for step in result["steps"]:
            self.assertEqual(
                set(step),
                {"start_soc", "end_soc", "duration_s", "current_a",
                 "input_energy_wh"},
            )

    def test_constant_current_completes_on_exact_target(self) -> None:
        # 100 Ah, 10 A, 3600 s steps -> 0.1 SoC per step. taper_start 0.2
        # keeps every step at max current and the final step lands exactly.
        payload = base_request(
            target_soc=0.3,
            max_steps=5,
            config=config(taper_start_soc=0.2),
        )
        result = self.service.plan_charging(payload)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["steps"]), 3)
        starts = [0.0, 0.1, 0.2]
        for index, step in enumerate(result["steps"]):
            self.assertAlmostEqual(step["start_soc"], starts[index])
            self.assertAlmostEqual(step["end_soc"], starts[index] + 0.1)
            self.assertEqual(step["duration_s"], 3600.0)
            self.assertEqual(step["current_a"], 10.0)
            self.assertAlmostEqual(step["input_energy_wh"], 400.0)
        self.assertAlmostEqual(result["final_soc"], 0.3)
        self.assertEqual(result["elapsed_s"], 10800.0)
        self.assertAlmostEqual(result["input_energy_wh"], 1200.0)

    def test_step_chaining_and_totals(self) -> None:
        result = self.service.plan_charging(base_request())
        for previous, current in zip(result["steps"], result["steps"][1:]):
            self.assertEqual(previous["end_soc"], current["start_soc"])
        self.assertAlmostEqual(
            result["elapsed_s"], sum(s["duration_s"] for s in result["steps"])
        )
        self.assertAlmostEqual(
            result["input_energy_wh"],
            sum(s["input_energy_wh"] for s in result["steps"]),
        )
        self.assertAlmostEqual(
            result["final_soc"], result["steps"][-1]["end_soc"]
        )

    def test_current_taper_is_linear_with_remaining_distance(self) -> None:
        result = self.service.plan_charging(base_request())
        # Steps at or below taper_start_soc use the maximum current.
        for index in range(6):
            self.assertEqual(result["steps"][index]["current_a"], 10.0)
        self.assertAlmostEqual(result["steps"][5]["start_soc"], 0.5)
        self.assertAlmostEqual(result["steps"][5]["end_soc"], 0.6)
        # First taper step starts at 0.6: 2 + (10 - 2) * 0.4 / 0.5 = 8.4
        self.assertAlmostEqual(result["steps"][6]["current_a"], 8.4)
        self.assertAlmostEqual(result["steps"][6]["end_soc"], 0.684)
        # Taper current stays strictly above the end current until the target.
        for step in result["steps"][6:]:
            self.assertGreater(step["current_a"], 2.0)

    def test_final_step_shortened_to_hit_target(self) -> None:
        result = self.service.plan_charging(base_request())
        last = result["steps"][-1]
        self.assertEqual(result["status"], "completed")
        self.assertAlmostEqual(result["final_soc"], 1.0)
        self.assertAlmostEqual(last["end_soc"], 1.0)
        self.assertLess(last["duration_s"], 3600.0)
        self.assertGreater(last["duration_s"], 0.0)
        expected_duration = (
            (1.0 - last["start_soc"]) * 100.0 * 3600.0
            / (last["current_a"] * 1.0)
        )
        self.assertAlmostEqual(last["duration_s"], expected_duration)
        self.assertAlmostEqual(
            last["input_energy_wh"],
            40.0 * last["current_a"] * last["duration_s"] / 3600.0,
        )

    def test_shortened_crossing_step_in_constant_current_region(self) -> None:
        payload = base_request(
            target_soc=0.12,
            step_duration_s=1800.0,
            max_steps=10,
            # Flat profile keeps the crossing step at the maximum current.
            config=config(taper_start_soc=0.05, taper_end_current_a=10.0),
        )
        result = self.service.plan_charging(payload)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["steps"]), 3)
        last = result["steps"][-1]
        self.assertAlmostEqual(last["start_soc"], 0.10)
        self.assertAlmostEqual(last["end_soc"], 0.12)
        self.assertAlmostEqual(last["duration_s"], 720.0)
        self.assertEqual(last["current_a"], 10.0)
        self.assertAlmostEqual(last["input_energy_wh"], 80.0)

    def test_shortened_crossing_step_in_taper_region(self) -> None:
        payload = base_request(target_soc=0.61, max_steps=10)
        result = self.service.plan_charging(payload)
        self.assertEqual(result["status"], "completed")
        last = result["steps"][-1]
        self.assertAlmostEqual(last["start_soc"], 0.6)
        # Remaining fraction: (0.61 - 0.6) / (0.61 - 0.5) = 1 / 11
        expected_current = 2.0 + 8.0 / 11.0
        self.assertAlmostEqual(last["current_a"], expected_current)
        self.assertAlmostEqual(last["end_soc"], 0.61)
        self.assertAlmostEqual(
            last["duration_s"], 0.01 * 360000.0 / expected_current
        )

    def test_incomplete_when_steps_run_out(self) -> None:
        result = self.service.plan_charging(base_request(max_steps=3))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["steps"]), 3)
        self.assertAlmostEqual(result["final_soc"], 0.3)
        self.assertEqual(result["elapsed_s"], 10800.0)
        self.assertLess(result["final_soc"], 1.0)

    def test_initial_equal_target_is_empty_completed_plan(self) -> None:
        payload = base_request(
            initial_soc=0.5,
            target_soc=0.5,
            max_steps=5,
            config=config(taper_start_soc=0.2),
        )
        result = self.service.plan_charging(payload)
        self.assertEqual(
            result,
            {
                "steps": [],
                "final_soc": 0.5,
                "elapsed_s": 0.0,
                "input_energy_wh": 0.0,
                "status": "completed",
            },
        )

    def test_coulombic_efficiency_scales_increment(self) -> None:
        payload = base_request(
            target_soc=0.1,
            max_steps=10,
            config=config(
                coulombic_efficiency=0.5,
                taper_start_soc=0.0,
                taper_end_current_a=10.0,
            ),
        )
        result = self.service.plan_charging(payload)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["steps"]), 2)
        self.assertAlmostEqual(result["steps"][0]["start_soc"], 0.0)
        self.assertAlmostEqual(result["steps"][0]["end_soc"], 0.05)
        self.assertAlmostEqual(result["steps"][0]["input_energy_wh"], 400.0)
        self.assertAlmostEqual(result["steps"][1]["end_soc"], 0.1)

    def test_current_evaluated_at_step_start(self) -> None:
        # Starting inside the taper region: current must follow the start SoC.
        payload = base_request(initial_soc=0.8, target_soc=1.0, max_steps=500)
        result = self.service.plan_charging(payload)
        # Remaining fraction: (1.0 - 0.8) / (1.0 - 0.5) = 0.4
        self.assertAlmostEqual(result["steps"][0]["current_a"], 2.0 + 8.0 * 0.4)
        self.assertEqual(result["status"], "completed")
        self.assertAlmostEqual(result["final_soc"], 1.0)

    def test_taper_start_at_zero_keeps_first_step_at_max(self) -> None:
        payload = base_request(
            target_soc=0.1,
            step_duration_s=360.0,
            max_steps=500,
            config=config(taper_start_soc=0.0),
        )
        result = self.service.plan_charging(payload)
        self.assertEqual(result["steps"][0]["current_a"], 10.0)
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
        for bad in (None, -0.1, 1.1, float("nan"), float("inf"), True, "0.5"):
            self.assert_code(base_request(initial_soc=bad), "invalid_soc_range")
            self.assert_code(base_request(target_soc=bad), "invalid_soc_range")
        self.assert_code(
            base_request(initial_soc=0.8, target_soc=0.5),
            "invalid_soc_range",
        )

    def test_config_rules(self) -> None:
        good = base_request()
        self.assert_code({**good, "config": None}, "invalid_charge_config")
        self.assert_code({**good, "config": "x"}, "invalid_charge_config")
        self.assert_code({**good, "config": {}}, "invalid_charge_config")

        for bad in (None, 0.0, -1.0, float("nan"), float("inf"), True, "10"):
            self.assert_code(
                base_request(config=config(max_current_a=bad)),
                "invalid_charge_config",
            )
            self.assert_code(
                base_request(config=config(charge_voltage_v=bad)),
                "invalid_charge_config",
            )

        for bad in (None, 0.0, -0.1, 1.1, float("nan"), float("inf"), True, "1"):
            self.assert_code(
                base_request(config=config(coulombic_efficiency=bad)),
                "invalid_charge_config",
            )
        # Efficiency of exactly 1 is allowed.
        result = self.service.plan_charging(
            base_request(config=config(coulombic_efficiency=1.0))
        )
        self.assertEqual(result["status"], "completed")

        for bad in (None, -0.1, 1.0, float("nan"), float("inf"), True, "0.5"):
            self.assert_code(
                base_request(config=config(taper_start_soc=bad)),
                "invalid_charge_config",
            )
        # taper_start_soc equal to the target is not allowed.
        self.assert_code(
            base_request(
                target_soc=0.5, config=config(taper_start_soc=0.5)
            ),
            "invalid_charge_config",
        )

        for bad in (None, 0.0, -1.0, 10.01, float("nan"), float("inf"), True, "2"):
            self.assert_code(
                base_request(config=config(taper_end_current_a=bad)),
                "invalid_charge_config",
            )
        # taper_end_current_a equal to max current is allowed.
        result = self.service.plan_charging(
            base_request(config=config(taper_end_current_a=10.0))
        )
        self.assertEqual(result["status"], "completed")

    def test_plan_option_rules(self) -> None:
        for bad in (None, 0.0, -1.0, float("nan"), float("inf"), True, "3600"):
            self.assert_code(
                base_request(step_duration_s=bad), "invalid_plan_options"
            )
        for bad in (None, 0, -1, True, 1.0, "5"):
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
            CHARGE_PLAN_ROUTE,
            json.dumps(base_request(target_soc=0.3, max_steps=5,
                                    config=config(taper_start_soc=0.2))).encode(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(len(body["steps"]), 3)
        self.assertAlmostEqual(body["final_soc"], 0.3)

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
        payload = base_request(capacity_ah=0)
        status, body = self.post(
            CHARGE_PLAN_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_capacity")

    def test_invalid_soc_range_is_422(self) -> None:
        payload = base_request(initial_soc=0.8, target_soc=0.5)
        status, body = self.post(
            CHARGE_PLAN_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_soc_range")

    def test_invalid_config_is_422(self) -> None:
        payload = base_request(config={})
        status, body = self.post(
            CHARGE_PLAN_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_charge_config")

    def test_invalid_plan_options_is_422(self) -> None:
        payload = base_request(max_steps=0)
        status, body = self.post(
            CHARGE_PLAN_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_plan_options")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/charge/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
