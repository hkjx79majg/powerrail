import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import (
    TELEMETRY_AGGREGATE_ROUTE,
    TELEMETRY_TREND_ROUTE,
    Handler,
)
from powerrail.service import ApiError, Service


def bucket(start, power=10.0, duration=10.0, covered=None):
    if covered is None:
        covered = duration
    return {
        "bucket_start_s": start,
        "bucket_end_s": start + duration,
        "covered_duration_s": covered,
        "average_power_w": power,
    }


def config(**overrides):
    value = {
        "baseline_window": 3,
        "recovery_windows": 2,
        "min_coverage_ratio": 0.5,
        "warning_deviation_w": 5.0,
        "critical_deviation_w": 10.0,
    }
    value.update(overrides)
    return value


def base_request(**overrides):
    payload = {
        "buckets": [bucket(0), bucket(10), bucket(20), bucket(30)],
        "config": config(),
    }
    payload.update(overrides)
    return payload


class TelemetryTrendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def analyze(self, buckets, **cfg):
        return self.service.analyze_telemetry_trend(
            {"buckets": buckets, "config": config(**cfg)}
        )

    def test_warming_up_until_history_fills_then_normal(self) -> None:
        result = self.analyze(
            [bucket(0, 10.0), bucket(10, 10.0), bucket(20, 10.0), bucket(30, 10.0)]
        )
        targets = [item["target_level"] for item in result["results"]]
        levels = [item["level"] for item in result["results"]]
        self.assertEqual(targets, ["warming_up", "warming_up", "warming_up", "normal"])
        self.assertEqual(levels, ["normal", "normal", "normal", "normal"])
        self.assertEqual(result["final_level"], "normal")
        # First window has no history; later warming windows still report values.
        self.assertIsNone(result["results"][0]["baseline_power_w"])
        self.assertIsNone(result["results"][0]["deviation_w"])
        self.assertEqual(result["results"][1]["baseline_power_w"], 10.0)
        self.assertEqual(result["results"][1]["deviation_w"], 0.0)
        self.assertEqual(result["results"][2]["baseline_power_w"], 10.0)
        self.assertEqual(result["results"][3]["baseline_power_w"], 10.0)
        self.assertEqual(result["results"][3]["deviation_w"], 0.0)

    def test_baseline_is_median_of_previous_window(self) -> None:
        # Four predecessors average the middle pair (10 and 12).
        result = self.analyze(
            [
                bucket(0, 8.0),
                bucket(10, 10.0),
                bucket(20, 12.0),
                bucket(30, 14.0),
                bucket(40, 20.0),
            ],
            baseline_window=4,
        )
        item = result["results"][4]
        self.assertEqual(item["target_level"], "warning")
        self.assertAlmostEqual(item["baseline_power_w"], 11.0)
        self.assertAlmostEqual(item["deviation_w"], 9.0)

    def test_baseline_window_uses_only_most_recent(self) -> None:
        result = self.analyze(
            [
                bucket(0, 100.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 10.0),
                bucket(40, 10.0),
            ],
            baseline_window=3,
        )
        # Window 4 baseline drops the 100 W outlier; window 5 stays on 10 W.
        self.assertEqual(result["results"][3]["target_level"], "normal")
        self.assertEqual(result["results"][3]["baseline_power_w"], 10.0)
        self.assertEqual(result["results"][4]["baseline_power_w"], 10.0)

    def test_warning_and_critical_escalate_immediately(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 16.0),
                bucket(40, 30.0),
            ]
        )
        items = result["results"]
        self.assertEqual(items[3]["target_level"], "warning")
        self.assertEqual(items[3]["level"], "warning")
        self.assertAlmostEqual(items[3]["deviation_w"], 6.0)
        self.assertEqual(items[4]["target_level"], "critical")
        self.assertEqual(items[4]["level"], "critical")
        self.assertEqual(result["final_level"], "critical")

    def test_deviation_threshold_is_inclusive(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 15.0),
            ]
        )
        self.assertEqual(result["results"][3]["target_level"], "warning")

    def test_escalation_can_jump_two_levels(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 50.0),
            ]
        )
        self.assertEqual(result["results"][3]["target_level"], "critical")
        self.assertEqual(result["results"][3]["level"], "critical")

    def test_recovery_drops_one_level_after_streak(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 50.0),   # -> critical
                bucket(40, 10.0),   # target normal, streak 1
                bucket(50, 10.0),   # streak 2 -> warning, reset
                bucket(60, 10.0),   # streak 1
                bucket(70, 10.0),   # streak 2 -> normal
            ]
        )
        levels = [item["level"] for item in result["results"]]
        self.assertEqual(
            levels,
            [
                "normal",
                "normal",
                "normal",
                "critical",
                "critical",
                "warning",
                "warning",
                "normal",
            ],
        )
        self.assertEqual(result["final_level"], "normal")

    def test_non_recovering_window_resets_streak(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 50.0),  # critical
                bucket(40, 10.0),  # streak 1
                bucket(50, 16.0),  # target warning < critical, streak 2
                bucket(60, 30.0),  # target critical, streak reset, stays critical
                bucket(70, 10.0),  # streak 1
                bucket(80, 10.0),  # streak 2 -> warning
            ]
        )
        self.assertEqual(result["results"][6]["level"], "critical")
        self.assertEqual(result["results"][8]["level"], "warning")

    def test_warning_recovers_to_normal(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 16.0),  # warning
                bucket(40, 10.0),
                bucket(50, 10.0),  # -> normal
            ]
        )
        self.assertEqual(result["results"][-1]["level"], "normal")

    def test_insufficient_window_holds_level_and_resets_streak(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 50.0),  # critical
                bucket(40, 10.0),  # streak 1
                bucket(50, 10.0, covered=2.0),  # ratio 0.2 < 0.5, invalid
                bucket(60, 10.0),  # streak restarts at 1
                bucket(70, 10.0),  # streak 2 -> warning
            ]
        )
        invalid = result["results"][5]
        self.assertEqual(invalid["target_level"], "insufficient")
        self.assertEqual(invalid["level"], "critical")
        self.assertIsNone(invalid["baseline_power_w"])
        self.assertIsNone(invalid["deviation_w"])
        self.assertAlmostEqual(invalid["coverage_ratio"], 0.2)
        self.assertEqual(result["results"][7]["level"], "warning")

    def test_zero_coverage_is_invalid(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 10.0, covered=0.0),
            ]
        )
        self.assertEqual(result["results"][3]["target_level"], "insufficient")

    def test_invalid_window_does_not_enter_history(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0, covered=0.0),  # invalid, not counted
                bucket(30, 10.0),
            ]
        )
        # Only two valid predecessors -> still warming up.
        self.assertEqual(result["results"][3]["target_level"], "warming_up")

    def test_coverage_ratio_boundary_is_inclusive(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, 10.0, covered=5.0),  # ratio exactly 0.5
            ]
        )
        self.assertEqual(result["results"][3]["target_level"], "normal")

    def test_min_coverage_zero_still_requires_positive_duration(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0, covered=0.0),
                bucket(30, 10.0),
            ],
            min_coverage_ratio=0.0,
        )
        self.assertEqual(result["results"][2]["target_level"], "insufficient")

    def test_results_echo_input_fields_in_order(self) -> None:
        buckets = [
            bucket(0, 8.0, duration=10.0, covered=10.0),
            bucket(20, 12.0, duration=5.0, covered=3.0),
            bucket(40, 9.0),
        ]
        result = self.analyze(buckets, baseline_window=2, min_coverage_ratio=0.5)
        self.assertEqual(len(result["results"]), 3)
        for item, source in zip(result["results"], buckets):
            self.assertEqual(item["bucket_start_s"], source["bucket_start_s"])
            self.assertEqual(item["bucket_end_s"], source["bucket_end_s"])
            self.assertEqual(
                item["covered_duration_s"], source["covered_duration_s"]
            )
            self.assertEqual(item["average_power_w"], source["average_power_w"])
        self.assertAlmostEqual(result["results"][1]["coverage_ratio"], 0.6)

    def test_negative_power_deviations_stay_normal(self) -> None:
        result = self.analyze(
            [
                bucket(0, 10.0),
                bucket(10, 10.0),
                bucket(20, 10.0),
                bucket(30, -50.0),
            ]
        )
        self.assertEqual(result["results"][3]["target_level"], "normal")
        self.assertAlmostEqual(result["results"][3]["deviation_w"], -60.0)

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.analyze_telemetry_trend(payload)
        self.assertEqual(payload, snapshot)


class TelemetryTrendValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.analyze_telemetry_trend(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_buckets_required_non_empty(self) -> None:
        self.assert_code({"config": config()}, "invalid_buckets")
        self.assert_code({"buckets": [], "config": config()}, "invalid_buckets")
        self.assert_code({"buckets": None, "config": config()}, "invalid_buckets")
        self.assert_code({"buckets": "x", "config": config()}, "invalid_buckets")
        self.assert_code(
            {"buckets": [bucket(0), "x"], "config": config()}, "invalid_buckets"
        )

    def test_bucket_field_rules(self) -> None:
        good = {"config": config()}
        for field, bad in (
            ("bucket_start_s", None),
            ("bucket_start_s", True),
            ("bucket_start_s", "0"),
            ("bucket_start_s", float("nan")),
            ("bucket_end_s", float("inf")),
            ("covered_duration_s", -0.1),
            ("average_power_w", False),
        ):
            broken = bucket(0)
            broken[field] = bad
            self.assert_code({"buckets": [broken], **good}, "invalid_buckets")

    def test_start_must_precede_end(self) -> None:
        broken = bucket(0)
        broken["bucket_end_s"] = 0.0
        self.assert_code({"buckets": [broken], "config": config()}, "invalid_buckets")

    def test_coverage_must_be_within_window(self) -> None:
        broken = bucket(0, duration=10.0, covered=10.0001)
        self.assert_code({"buckets": [broken], "config": config()}, "invalid_buckets")

    def test_starts_must_be_strictly_increasing(self) -> None:
        self.assert_code(
            {"buckets": [bucket(10), bucket(10)], "config": config()},
            "invalid_buckets",
        )
        self.assert_code(
            {"buckets": [bucket(20), bucket(10)], "config": config()},
            "invalid_buckets",
        )

    def test_windows_must_not_overlap(self) -> None:
        first = bucket(0, duration=10.0)
        second = bucket(5, duration=10.0)
        self.assert_code(
            {"buckets": [first, second], "config": config()}, "invalid_buckets"
        )

    def test_adjacent_windows_are_allowed(self) -> None:
        result = self.service.analyze_telemetry_trend(
            {
                "buckets": [
                    bucket(0, 10.0, duration=10.0),
                    bucket(10, 10.0, duration=10.0),
                    bucket(20, 10.0, duration=10.0),
                ],
                "config": config(baseline_window=2),
            }
        )
        self.assertEqual(result["results"][-1]["target_level"], "normal")

    def test_config_required(self) -> None:
        self.assert_code({"buckets": [bucket(0)]}, "invalid_trend_config")
        self.assert_code(
            {"buckets": [bucket(0)], "config": None}, "invalid_trend_config"
        )
        self.assert_code(
            {"buckets": [bucket(0)], "config": []}, "invalid_trend_config"
        )

    def test_window_counts_must_be_positive_integers(self) -> None:
        for bad in (0, -1, 1.5, True, "3", None):
            self.assert_code(
                {"buckets": [bucket(0)], "config": config(baseline_window=bad)},
                "invalid_trend_config",
            )
            self.assert_code(
                {"buckets": [bucket(0)], "config": config(recovery_windows=bad)},
                "invalid_trend_config",
            )

    def test_coverage_ratio_rules(self) -> None:
        for bad in (-0.01, 1.01, True, "0.5", None, float("nan")):
            self.assert_code(
                {"buckets": [bucket(0)], "config": config(min_coverage_ratio=bad)},
                "invalid_trend_config",
            )

    def test_threshold_rules(self) -> None:
        for bad in (-0.1, True, "5", None, float("nan"), float("inf")):
            self.assert_code(
                {"buckets": [bucket(0)], "config": config(warning_deviation_w=bad)},
                "invalid_trend_config",
            )
            self.assert_code(
                {"buckets": [bucket(0)], "config": config(critical_deviation_w=bad)},
                "invalid_trend_config",
            )

    def test_threshold_ordering(self) -> None:
        self.assert_code(
            {"buckets": [bucket(0)], "config": config(warning_deviation_w=10.0,
                                                     critical_deviation_w=10.0)},
            "invalid_trend_config",
        )
        self.assert_code(
            {"buckets": [bucket(0)], "config": config(warning_deviation_w=11.0,
                                                     critical_deviation_w=10.0)},
            "invalid_trend_config",
        )

    def test_thresholds_accept_zero_warning(self) -> None:
        result = self.service.analyze_telemetry_trend(
            {
                "buckets": [bucket(0, 10.0), bucket(10, 10.0), bucket(20, 9.0)],
                "config": config(
                    baseline_window=2,
                    warning_deviation_w=0.0,
                    critical_deviation_w=5.0,
                ),
            }
        )
        # Deviation -1 W is below the zero warning threshold.
        self.assertEqual(result["results"][-1]["target_level"], "normal")


class TelemetryTrendHttpTest(unittest.TestCase):
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

    def test_trend_route_returns_200(self) -> None:
        status, body = self.post(
            TELEMETRY_TREND_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 4)
        self.assertEqual(body["final_level"], "normal")
        self.assertEqual(
            set(body["results"][0]),
            {
                "bucket_start_s",
                "bucket_end_s",
                "covered_duration_s",
                "coverage_ratio",
                "average_power_w",
                "baseline_power_w",
                "deviation_w",
                "target_level",
                "level",
            },
        )

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_TREND_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_TREND_ROUTE, b"{bad")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_TREND_ROUTE, b"123")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_buckets_is_422(self) -> None:
        status, body = self.post(
            TELEMETRY_TREND_ROUTE,
            json.dumps({"buckets": [{"bucket_start_s": 0}], "config": config()}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_buckets")

    def test_invalid_config_is_422(self) -> None:
        bad = dict(config())
        bad["baseline_window"] = 0
        status, body = self.post(
            TELEMETRY_TREND_ROUTE,
            json.dumps({"buckets": [bucket(0)], "config": bad}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_trend_config")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/telemetry/trend/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_existing_aggregate_route_still_works(self) -> None:
        payload = {
            "samples": [
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 10.0},
                {"timestamp_s": 1, "current_a": 1.0, "voltage_v": 10.0},
            ],
            "bucket_duration_s": 2.0,
            "max_gap_s": 5.0,
        }
        status, body = self.post(
            TELEMETRY_AGGREGATE_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["buckets"]), 1)


if __name__ == "__main__":
    unittest.main()
