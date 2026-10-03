import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import ENERGY_BENCHMARK_ROUTE, Handler
from powerrail.service import ApiError, Service


def run(energy, duration):
    return {"energy_wh": energy, "duration_s": duration}


def scenario(scenario_id, baseline_runs=None, current_runs=None):
    if baseline_runs is None:
        baseline_runs = [run(1.0, 360.0)]
    if current_runs is None:
        current_runs = [run(1.0, 360.0)]
    return {
        "id": scenario_id,
        "baseline_runs": baseline_runs,
        "current_runs": current_runs,
    }


def base_request(**overrides):
    payload = {
        "scenarios": [
            scenario("idle", [run(1.0, 360.0)], [run(1.0, 360.0)]),
            scenario("active", [run(2.0, 360.0)], [run(2.4, 360.0)]),
        ],
    }
    payload.update(overrides)
    return payload


class EnergyBenchmarkCompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_results_follow_input_order_with_expected_fields(self) -> None:
        result = self.service.compare_energy_benchmark(base_request())
        self.assertEqual(
            [item["id"] for item in result["results"]], ["idle", "active"]
        )
        for item in result["results"]:
            self.assertEqual(
                set(item),
                {
                    "id",
                    "baseline_power_w",
                    "current_power_w",
                    "delta_power_w",
                    "change_percent",
                    "status",
                },
            )

    def test_power_is_energy_over_duration_averaged_per_group(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario(
                        "mixed",
                        [run(1.0, 360.0), run(3.0, 360.0)],
                        [run(2.0, 180.0), run(4.0, 720.0)],
                    )
                ]
            )
        )
        item = result["results"][0]
        self.assertAlmostEqual(item["baseline_power_w"], 20.0)
        self.assertAlmostEqual(item["current_power_w"], 30.0)
        self.assertAlmostEqual(item["delta_power_w"], 10.0)
        self.assertAlmostEqual(item["change_percent"], 50.0)

    def test_stable_within_default_threshold(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario("edge-up", [run(1.0, 360.0)], [run(1.05, 360.0)]),
                    scenario("edge-down", [run(1.0, 360.0)], [run(0.95, 360.0)]),
                ]
            )
        )
        self.assertEqual(result["results"][0]["status"], "stable")
        self.assertEqual(result["results"][1]["status"], "stable")
        self.assertEqual(result["overall_status"], "stable")
        self.assertEqual(result["regression_count"], 0)
        self.assertEqual(result["improvement_count"], 0)

    def test_strict_bounds_beyond_threshold(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario("up", [run(1.0, 360.0)], [run(1.050001, 360.0)]),
                    scenario("down", [run(1.0, 360.0)], [run(0.949999, 360.0)]),
                ]
            )
        )
        self.assertEqual(result["results"][0]["status"], "regression")
        self.assertEqual(result["results"][1]["status"], "improvement")
        self.assertEqual(result["regression_count"], 1)
        self.assertEqual(result["improvement_count"], 1)
        self.assertEqual(result["overall_status"], "regression")

    def test_custom_threshold(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[scenario("s", [run(1.0, 360.0)], [run(1.2, 360.0)])],
                regression_threshold_percent=25.0,
            )
        )
        self.assertEqual(result["results"][0]["status"], "stable")

    def test_zero_threshold_marks_any_change(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario("up", [run(1.0, 360.0)], [run(1.000001, 360.0)]),
                    scenario("down", [run(1.0, 360.0)], [run(0.999999, 360.0)]),
                ],
                regression_threshold_percent=0,
            )
        )
        self.assertEqual(result["results"][0]["status"], "regression")
        self.assertEqual(result["results"][1]["status"], "improvement")

    def test_zero_baseline_with_zero_current_is_stable(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[scenario("zero", [run(0.0, 60.0)], [run(0.0, 60.0)])]
            )
        )
        item = result["results"][0]
        self.assertEqual(item["baseline_power_w"], 0.0)
        self.assertEqual(item["current_power_w"], 0.0)
        self.assertEqual(item["change_percent"], 0.0)
        self.assertEqual(item["status"], "stable")

    def test_zero_baseline_with_positive_current_is_regression(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[scenario("zero", [run(0.0, 60.0)], [run(0.5, 60.0)])]
            )
        )
        item = result["results"][0]
        self.assertIsNone(item["change_percent"])
        self.assertEqual(item["status"], "regression")
        self.assertEqual(result["overall_status"], "regression")

    def test_overall_status_prioritizes_regression_over_improvement(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario("better", [run(1.0, 360.0)], [run(0.5, 360.0)]),
                    scenario("worse", [run(1.0, 360.0)], [run(2.0, 360.0)]),
                ]
            )
        )
        self.assertEqual(result["regression_count"], 1)
        self.assertEqual(result["improvement_count"], 1)
        self.assertEqual(result["overall_status"], "regression")

    def test_overall_improvement_without_regression(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario("better", [run(1.0, 360.0)], [run(0.5, 360.0)]),
                    scenario("same", [run(1.0, 360.0)], [run(1.0, 360.0)]),
                ]
            )
        )
        self.assertEqual(result["overall_status"], "improvement")

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.compare_energy_benchmark(payload)
        self.assertEqual(payload, snapshot)


class EnergyBenchmarkValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_error(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.compare_energy_benchmark(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5, 1.5, True):
            self.assert_error(payload, "invalid_json", status=400)

    def test_scenarios_must_be_a_non_empty_array_of_objects(self) -> None:
        self.assert_error(base_request(scenarios=None), "invalid_scenarios")
        self.assert_error(base_request(scenarios="x"), "invalid_scenarios")
        self.assert_error(base_request(scenarios=[]), "invalid_scenarios")
        self.assert_error(base_request(scenarios=["x"]), "invalid_scenarios")
        self.assert_error(base_request(scenarios=[None]), "invalid_scenarios")

    def test_scenario_id_rules(self) -> None:
        for bad in (None, "", 5, True):
            broken = scenario("ok")
            broken["id"] = bad
            self.assert_error(
                base_request(scenarios=[broken]), "invalid_scenario_id"
            )
        self.assert_error(
            base_request(scenarios=[scenario("dup"), scenario("dup")]),
            "invalid_scenario_id",
        )

    def test_runs_must_be_non_empty_arrays_of_objects(self) -> None:
        for key in ("baseline_runs", "current_runs"):
            for bad in (None, "x", [], [None], ["x"]):
                broken = scenario("ok")
                broken[key] = bad
                self.assert_error(
                    base_request(scenarios=[broken]), "invalid_runs"
                )

    def test_run_measurement_rules(self) -> None:
        for bad in (None, -0.1, "x", True, float("nan"), float("inf")):
            broken = scenario("ok", baseline_runs=[run(bad, 60.0)])
            self.assert_error(
                base_request(scenarios=[broken]), "invalid_run_measurement"
            )
        for bad in (None, 0.0, -1.0, "x", True, float("nan"), float("inf")):
            broken = scenario("ok", current_runs=[run(1.0, bad)])
            self.assert_error(
                base_request(scenarios=[broken]), "invalid_run_measurement"
            )

    def test_threshold_rules(self) -> None:
        for bad in (None, -0.1, "x", True, float("nan"), float("inf")):
            self.assert_error(
                base_request(regression_threshold_percent=bad), "invalid_options"
            )


class EnergyBenchmarkHttpTest(unittest.TestCase):
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

    def post(self, raw, path=ENERGY_BENCHMARK_ROUTE):
        request = urllib.request.Request(
            self.base + path,
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

    def test_compare_route_returns_200(self) -> None:
        status, body = self.post(json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 2)
        self.assertIn("regression_count", body)
        self.assertIn("improvement_count", body)
        self.assertIn("overall_status", body)

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

    def test_invalid_scenarios_is_422(self) -> None:
        status, body = self.post(json.dumps({"scenarios": []}).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_scenarios")

    def test_invalid_run_measurement_is_422(self) -> None:
        payload = base_request(
            scenarios=[scenario("ok", baseline_runs=[run(-1.0, 60.0)])]
        )
        status, body = self.post(json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_run_measurement")

    def test_invalid_options_is_422(self) -> None:
        payload = base_request(regression_threshold_percent=-1.0)
        status, body = self.post(json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_options")

    def test_unknown_path_is_not_found(self) -> None:
        status, body = self.post(b"{}", path="/v1/energy/benchmark/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
