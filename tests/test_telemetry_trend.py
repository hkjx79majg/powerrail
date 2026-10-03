import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import TELEMETRY_AGGREGATE_ROUTE, TELEMETRY_TREND_ROUTE, Handler
from powerrail.service import ApiError, Service


def bucket(start, end, covered=None, power=10.0):
    if covered is None:
        covered = end - start
    return {
        "bucket_start_s": start,
        "bucket_end_s": end,
        "covered_duration_s": covered,
        "average_power_w": power,
    }


def config(**overrides):
    values = {
        "baseline_window": 2,
        "recovery_windows": 2,
        "min_coverage_ratio": 0.5,
        "warning_deviation_w": 5.0,
        "critical_deviation_w": 10.0,
    }
    values.update(overrides)
    return values


def request(powers, covereds=None, **config_overrides):
    if covereds is None:
        covereds = [10.0] * len(powers)
    return {
        "buckets": [
            bucket(index * 10, index * 10 + 10, covereds[index], powers[index])
            for index in range(len(powers))
        ],
        "config": config(**config_overrides),
    }


class TelemetryTrendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_warming_up_until_history_fills(self) -> None:
        result = self.service.analyze_telemetry_trend(request([10.0, 12.0]))
        first, second = result["results"]
        self.assertEqual(first["target_level"], "warming_up")
        self.assertIsNone(first["baseline_power_w"])
        self.assertIsNone(first["deviation_w"])
        self.assertEqual(first["level"], "normal")
        self.assertEqual(second["target_level"], "warming_up")
        self.assertEqual(second["level"], "normal")
        self.assertEqual(result["final_level"], "normal")

    def test_baseline_is_median_of_previous_valid_powers(self) -> None:
        result = self.service.analyze_telemetry_trend(request([10.0, 12.0, 13.0]))
        # Even history averages the middle pair: median(10, 12) == 11.
        scored = result["results"][2]
        self.assertEqual(scored["baseline_power_w"], 11.0)
        self.assertEqual(scored["deviation_w"], 2.0)
        self.assertEqual(scored["target_level"], "normal")

    def test_warning_and_critical_escalation_is_immediate(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request([10.0, 10.0, 16.0, 25.0])
        )
        levels = [
            (item["target_level"], item["level"]) for item in result["results"]
        ]
        self.assertEqual(levels[2], ("warning", "warning"))
        self.assertEqual(levels[3], ("critical", "critical"))
        self.assertEqual(result["final_level"], "critical")

    def test_escalation_can_jump_two_levels(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request([10.0, 10.0, 40.0])
        )
        self.assertEqual(result["results"][2]["target_level"], "critical")
        self.assertEqual(result["results"][2]["level"], "critical")

    def test_recovery_drops_one_level_and_restarts_count(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request([10.0, 10.0, 30.0, 10.0, 10.0, 10.0, 10.0])
        )
        levels = [item["level"] for item in result["results"]]
        # Critical at index 2; two lower targets drop one level to warning...
        self.assertEqual(levels[4], "warning")
        # ...two more drop it back to normal.
        self.assertEqual(levels[6], "normal")
        self.assertEqual(result["final_level"], "normal")

    def test_non_recovering_window_resets_streak(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request([10.0, 10.0, 30.0, 10.0, 30.0, 10.0, 10.0])
        )
        levels = [item["level"] for item in result["results"]]
        # Streak of one at index 3 is reset by the critical target at index 4,
        # so indices 5-6 are the first two consecutive lower targets.
        self.assertEqual(levels[3], "critical")
        self.assertEqual(levels[4], "critical")
        self.assertEqual(levels[6], "warning")

    def test_invalid_window_is_insufficient_and_holds_level(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request([10.0, 10.0, 30.0, 9.0], covereds=[10, 10, 10, 0])
        )
        invalid = result["results"][3]
        self.assertEqual(invalid["target_level"], "insufficient")
        self.assertIsNone(invalid["baseline_power_w"])
        self.assertIsNone(invalid["deviation_w"])
        self.assertEqual(invalid["level"], "critical")
        self.assertEqual(result["final_level"], "critical")

    def test_invalid_window_does_not_enter_history_or_break_streak_rule(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request(
                [10.0, 10.0, 30.0, 9.0, 10.0, 10.0],
                covereds=[10, 10, 10, 0, 10, 10],
            )
        )
        levels = [item["level"] for item in result["results"]]
        # Invalid window resets the streak; only indices 4-5 count toward
        # recovery, dropping critical one level to warning.
        self.assertEqual(levels[5], "warning")
        # Baseline at index 4 still comes from 10 and 30 (gap skipped).
        self.assertEqual(result["results"][4]["baseline_power_w"], 20.0)

    def test_coverage_ratio_bounds(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request([10.0, 10.0], covereds=[10.0, 5.0])
        )
        self.assertEqual(result["results"][0]["coverage_ratio"], 1.0)
        self.assertEqual(result["results"][1]["coverage_ratio"], 0.5)
        self.assertEqual(result["results"][1]["target_level"], "warming_up")

        rejected = self.service.analyze_telemetry_trend(
            request([10.0, 10.0], covereds=[10.0, 4.0])
        )
        self.assertEqual(rejected["results"][1]["target_level"], "insufficient")

    def test_zero_coverage_is_invalid_even_without_floor(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request([10.0, 99.0], covereds=[10.0, 0.0], min_coverage_ratio=0.0)
        )
        self.assertEqual(result["results"][1]["target_level"], "insufficient")

    def test_results_echo_window_fields_in_input_order(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request([10.0, 12.0, 13.0])
        )
        self.assertEqual(
            set(result["results"][0]),
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
        for index, item in enumerate(result["results"]):
            self.assertEqual(item["bucket_start_s"], float(index * 10))
            self.assertEqual(item["bucket_end_s"], float(index * 10 + 10))
            self.assertEqual(item["covered_duration_s"], 10.0)
            self.assertEqual(item["average_power_w"], [10.0, 12.0, 13.0][index])

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = request([10.0, 12.0, 13.0])
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

    def test_buckets_required_and_non_empty(self) -> None:
        self.assert_code({}, "invalid_buckets")
        self.assert_code({"buckets": []}, "invalid_buckets")
        self.assert_code({"buckets": None}, "invalid_buckets")
        self.assert_code({"buckets": "x"}, "invalid_buckets")
        self.assert_code({"buckets": [1]}, "invalid_buckets")

    def test_bucket_fields_must_be_finite_non_boolean_numbers(self) -> None:
        for bad in (None, "1", True, float("nan"), float("inf")):
            self.assert_code(
                {"buckets": [bucket(0, 10, power=bad)], "config": config()},
                "invalid_buckets",
            )
        missing_field = bucket(0, 10)
        del missing_field["bucket_end_s"]
        self.assert_code(
            {"buckets": [missing_field], "config": config()}, "invalid_buckets"
        )

    def test_start_must_be_less_than_end(self) -> None:
        self.assert_code(
            {"buckets": [bucket(10, 10)], "config": config()}, "invalid_buckets"
        )
        self.assert_code(
            {"buckets": [bucket(11, 10)], "config": config()}, "invalid_buckets"
        )

    def test_starts_strictly_increasing_and_non_overlapping(self) -> None:
        self.assert_code(
            {"buckets": [bucket(0, 10), bucket(0, 12)], "config": config()},
            "invalid_buckets",
        )
        self.assert_code(
            {"buckets": [bucket(0, 10), bucket(5, 15)], "config": config()},
            "invalid_buckets",
        )

    def test_touching_windows_are_allowed(self) -> None:
        result = self.service.analyze_telemetry_trend(
            {"buckets": [bucket(0, 10), bucket(10, 20)], "config": config()}
        )
        self.assertEqual(len(result["results"]), 2)

    def test_covered_duration_bounds(self) -> None:
        self.assert_code(
            {"buckets": [bucket(0, 10, covered=-0.1)], "config": config()},
            "invalid_buckets",
        )
        self.assert_code(
            {"buckets": [bucket(0, 10, covered=10.1)], "config": config()},
            "invalid_buckets",
        )

    def test_config_required(self) -> None:
        self.assert_code({"buckets": [bucket(0, 10)]}, "invalid_trend_config")
        self.assert_code(
            {"buckets": [bucket(0, 10)], "config": None}, "invalid_trend_config"
        )

    def test_baseline_window_must_be_positive_integer(self) -> None:
        for bad in (None, 0, -1, 1.0, True, "2"):
            self.assert_code(
                {"buckets": [bucket(0, 10)], "config": config(baseline_window=bad)},
                "invalid_trend_config",
            )

    def test_recovery_windows_must_be_positive_integer(self) -> None:
        for bad in (None, 0, -1, 2.0, False, "2"):
            self.assert_code(
                {"buckets": [bucket(0, 10)], "config": config(recovery_windows=bad)},
                "invalid_trend_config",
            )

    def test_min_coverage_ratio_bounds(self) -> None:
        for bad in (None, -0.01, 1.01, True, "0.5", float("nan")):
            self.assert_code(
                {
                    "buckets": [bucket(0, 10)],
                    "config": config(min_coverage_ratio=bad),
                },
                "invalid_trend_config",
            )

    def test_deviation_threshold_rules(self) -> None:
        for warning, critical in [
            (-0.1, 10.0),
            (5.0, 5.0),
            (6.0, 5.0),
            (True, 10.0),
            (5.0, float("inf")),
            (float("nan"), 10.0),
        ]:
            self.assert_code(
                {
                    "buckets": [bucket(0, 10)],
                    "config": config(
                        warning_deviation_w=warning,
                        critical_deviation_w=critical,
                    ),
                },
                "invalid_trend_config",
            )

    def test_zero_warning_threshold_is_allowed(self) -> None:
        result = self.service.analyze_telemetry_trend(
            request(
                [10.0, 10.0, 10.1],
                warning_deviation_w=0.0,
                critical_deviation_w=5.0,
            )
        )
        self.assertEqual(result["results"][2]["target_level"], "warning")


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
        request_obj = urllib.request.Request(
            self.base + route,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request_obj) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read())
            exc.close()
            return exc.code, body

    def test_trend_route_returns_200(self) -> None:
        payload = request([10.0, 10.0, 30.0])
        status, body = self.post(
            TELEMETRY_TREND_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 3)
        self.assertEqual(body["final_level"], "critical")

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_TREND_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_TREND_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_non_object_json_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_TREND_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_buckets_is_422(self) -> None:
        status, body = self.post(
            TELEMETRY_TREND_ROUTE,
            json.dumps({"buckets": [bucket(2, 1)], "config": config()}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_buckets")

    def test_invalid_config_is_422(self) -> None:
        status, body = self.post(
            TELEMETRY_TREND_ROUTE,
            json.dumps({"buckets": [bucket(0, 10)], "config": {}}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_trend_config")

    def test_unknown_post_path_is_404(self) -> None:
        status, body = self.post("/v1/telemetry/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_existing_aggregate_route_unchanged(self) -> None:
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
