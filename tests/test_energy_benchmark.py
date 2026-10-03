import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import ENERGY_BENCHMARK_COMPARE_ROUTE, Handler
from powerrail.service import ApiError, Service


def run(energy_wh=10.0, duration_s=3600.0):
    return {"energy_wh": energy_wh, "duration_s": duration_s}


def scenario(
    scenario_id="s1",
    baseline=(run(10.0, 3600.0),),
    current=(run(10.0, 3600.0),),
    **overrides,
):
    payload = {
        "id": scenario_id,
        "baseline_runs": list(baseline),
        "current_runs": list(current),
    }
    payload.update(overrides)
    return payload


def base_request(**overrides):
    payload = {"scenarios": [scenario()]}
    payload.update(overrides)
    return payload


class EnergyBenchmarkCompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_run_power_is_energy_times_3600_over_duration(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario(
                        baseline=[run(10.0, 3600.0), run(5.0, 900.0)],
                        current=[run(20.0, 1800.0)],
                    )
                ]
            )
        )
        item = result["results"][0]
        # (10*3600/3600 + 5*3600/900) / 2 = (10 + 20) / 2
        self.assertAlmostEqual(item["baseline_power_w"], 15.0)
        self.assertAlmostEqual(item["current_power_w"], 40.0)
        self.assertAlmostEqual(item["delta_power_w"], 25.0)

    def test_results_match_input_order_and_fields(self) -> None:
        request = {
            "scenarios": [
                scenario("z", current=[run(9.0, 3600.0)]),
                scenario("a", current=[run(11.0, 3600.0)]),
                scenario("m", current=[run(10.0, 3600.0)]),
            ]
        }
        result = self.service.compare_energy_benchmark(request)
        self.assertEqual([item["id"] for item in result["results"]], ["z", "a", "m"])
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
        self.assertEqual(set(result), {"results", "regression_count",
                                       "improvement_count", "overall_status"})

    def test_change_percent_relative_to_baseline(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[scenario(current=[run(12.0, 3600.0)])]
            )
        )
        item = result["results"][0]
        self.assertAlmostEqual(item["change_percent"], 20.0)
        self.assertEqual(item["status"], "regression")

    def test_both_zero_power_is_zero_percent_and_stable(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario(
                        baseline=[run(0.0, 3600.0)],
                        current=[run(0.0, 10.0)],
                    )
                ]
            )
        )
        item = result["results"][0]
        self.assertEqual(item["baseline_power_w"], 0.0)
        self.assertEqual(item["current_power_w"], 0.0)
        self.assertEqual(item["delta_power_w"], 0.0)
        self.assertEqual(item["change_percent"], 0.0)
        self.assertEqual(item["status"], "stable")

    def test_baseline_zero_with_positive_current_is_null_regression(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[
                    scenario(
                        baseline=[run(0.0, 3600.0)],
                        current=[run(0.001, 3600.0)],
                    )
                ]
            )
        )
        item = result["results"][0]
        self.assertIsNone(item["change_percent"])
        self.assertEqual(item["status"], "regression")

    def test_threshold_boundaries_are_strict(self) -> None:
        def status_for(current_energy, threshold=5.0):
            result = self.service.compare_energy_benchmark(
                {
                    "regression_threshold_percent": threshold,
                    "scenarios": [
                        scenario(
                            baseline=[run(100.0, 3600.0)],
                            current=[run(current_energy, 3600.0)],
                        )
                    ],
                }
            )
            return result["results"][0]["status"]

        self.assertEqual(status_for(105.0), "stable")
        self.assertEqual(status_for(95.0), "stable")
        self.assertEqual(status_for(105.0 + 1e-9), "regression")
        self.assertEqual(status_for(95.0 - 1e-9), "improvement")

    def test_zero_threshold_bounds_equal_baseline(self) -> None:
        result = self.service.compare_energy_benchmark(
            {
                "regression_threshold_percent": 0,
                "scenarios": [
                    scenario(
                        baseline=[run(10.0, 3600.0)],
                        current=[run(10.0, 3600.0)],
                    )
                ],
            }
        )
        self.assertEqual(result["results"][0]["status"], "stable")

    def test_overall_status_prefers_regression_then_improvement(self) -> None:
        mixed = self.service.compare_energy_benchmark(
            {
                "scenarios": [
                    scenario("up", current=[run(12.0, 3600.0)]),
                    scenario("down", current=[run(8.0, 3600.0)]),
                ]
            }
        )
        self.assertEqual(mixed["regression_count"], 1)
        self.assertEqual(mixed["improvement_count"], 1)
        self.assertEqual(mixed["overall_status"], "regression")

        improved = self.service.compare_energy_benchmark(
            {
                "scenarios": [
                    scenario("down", current=[run(8.0, 3600.0)]),
                    scenario("flat", current=[run(10.0, 3600.0)]),
                ]
            }
        )
        self.assertEqual(improved["regression_count"], 0)
        self.assertEqual(improved["improvement_count"], 1)
        self.assertEqual(improved["overall_status"], "improvement")

        stable = self.service.compare_energy_benchmark(base_request())
        self.assertEqual(stable["regression_count"], 0)
        self.assertEqual(stable["improvement_count"], 0)
        self.assertEqual(stable["overall_status"], "stable")

    def test_default_threshold_is_five_percent(self) -> None:
        result = self.service.compare_energy_benchmark(
            base_request(
                scenarios=[scenario(current=[run(10.5, 3600.0)])]
            )
        )
        self.assertEqual(result["results"][0]["status"], "stable")

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

    def test_scenarios_must_be_non_empty_object_array(self) -> None:
        self.assert_error({}, "invalid_scenarios")
        self.assert_error(base_request(scenarios=None), "invalid_scenarios")
        self.assert_error(base_request(scenarios="x"), "invalid_scenarios")
        self.assert_error(base_request(scenarios=[]), "invalid_scenarios")
        self.assert_error(base_request(scenarios=["x"]), "invalid_scenarios")
        self.assert_error(base_request(scenarios=[scenario(), None]), "invalid_scenarios")

    def test_scenario_id_rules(self) -> None:
        for bad_id in (None, "", 5, 1.5, True, []):
            self.assert_error(
                base_request(scenarios=[scenario(bad_id)]), "invalid_scenario_id"
            )
        self.assert_error(
            base_request(
                scenarios=[scenario("dup"), scenario("dup", current=[run(9.0)])]
            ),
            "invalid_scenario_id",
        )

    def test_runs_must_be_non_empty_object_arrays(self) -> None:
        for key in ("baseline_runs", "current_runs"):
            for bad_runs in (None, "x", [], ["x"], [run(), None]):
                broken = scenario()
                broken[key] = bad_runs
                self.assert_error(
                    base_request(scenarios=[broken]), "invalid_runs"
                )

    def test_run_measurement_rules(self) -> None:
        for key in ("energy_wh", "duration_s"):
            for bad in (None, "x", True, float("nan"), float("inf")):
                broken = run()
                broken[key] = bad
                self.assert_error(
                    base_request(scenarios=[scenario(current=[broken])]),
                    "invalid_run_measurement",
                )
        for bad_energy in (-0.1, -1.0):
            self.assert_error(
                base_request(scenarios=[scenario(current=[run(bad_energy, 3600.0)])]),
                "invalid_run_measurement",
            )
        for bad_duration in (0.0, -1.0):
            self.assert_error(
                base_request(scenarios=[scenario(current=[run(10.0, bad_duration)])]),
                "invalid_run_measurement",
            )

    def test_threshold_rules(self) -> None:
        for bad in (None, -0.1, "x", True, float("nan"), float("inf")):
            self.assert_error(
                base_request(regression_threshold_percent=bad),
                "invalid_options",
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

    def post(self, raw, path=ENERGY_BENCHMARK_COMPARE_ROUTE):
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
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["id"], "s1")
        self.assertIn("regression_count", body)
        self.assertIn("improvement_count", body)
        self.assertEqual(body["overall_status"], "stable")

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")
        self.assertTrue(body["error"]["message"])

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_top_level_array_is_invalid_json(self) -> None:
        status, body = self.post(b"[]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_scenarios_is_422(self) -> None:
        status, body = self.post(json.dumps({}).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_scenarios")

    def test_invalid_scenario_id_is_422(self) -> None:
        payload = base_request(scenarios=[scenario("")])
        status, body = self.post(json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_scenario_id")

    def test_invalid_runs_is_422(self) -> None:
        broken = scenario()
        broken["baseline_runs"] = []
        status, body = self.post(json.dumps(base_request(scenarios=[broken])).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_runs")

    def test_invalid_run_measurement_is_422(self) -> None:
        broken = run()
        broken["duration_s"] = True
        payload = base_request(scenarios=[scenario(current=[broken])])
        status, body = self.post(json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_run_measurement")

    def test_invalid_options_is_422(self) -> None:
        payload = base_request(regression_threshold_percent=-1.0)
        status, body = self.post(json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_options")

    def test_unknown_path_is_not_found(self) -> None:
        status, body = self.post(
            json.dumps(base_request()).encode(), path="/v1/energy/benchmark/unknown"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
