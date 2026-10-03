import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import TELEMETRY_AGGREGATE_ROUTE, TELEMETRY_FILTER_ROUTE, Handler
from powerrail.service import ApiError, Service


def sample(timestamp, current=1.0, voltage=10.0):
    return {"timestamp_s": timestamp, "current_a": current, "voltage_v": voltage}


def base_request(**overrides):
    payload = {
        "samples": [
            sample(0, 1.0, 10.0),
            sample(1, 1.0, 10.0),
            sample(2, 1.0, 10.0),
            sample(3, 1.0, 10.0),
        ],
        "bucket_duration_s": 2.0,
        "max_gap_s": 5.0,
    }
    payload.update(overrides)
    return payload


class TelemetryAggregateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_constant_power_splits_into_fixed_windows(self) -> None:
        result = self.service.aggregate_telemetry(base_request())
        self.assertEqual(result["skipped_gap_count"], 0)
        self.assertEqual(len(result["buckets"]), 2)
        first, second = result["buckets"]
        self.assertEqual(
            set(first),
            {
                "bucket_start_s",
                "bucket_end_s",
                "covered_duration_s",
                "average_power_w",
                "discharge_energy_wh",
                "charge_energy_wh",
                "net_energy_wh",
            },
        )
        self.assertEqual(first["bucket_start_s"], 0.0)
        self.assertEqual(first["bucket_end_s"], 2.0)
        self.assertEqual(first["covered_duration_s"], 2.0)
        self.assertAlmostEqual(first["discharge_energy_wh"], 20.0 / 3600.0)
        self.assertEqual(first["charge_energy_wh"], 0.0)
        self.assertAlmostEqual(first["net_energy_wh"], 20.0 / 3600.0)
        self.assertAlmostEqual(first["average_power_w"], 10.0)
        # Last window is capped at the final timestamp.
        self.assertEqual(second["bucket_start_s"], 2.0)
        self.assertEqual(second["bucket_end_s"], 3.0)
        self.assertEqual(second["covered_duration_s"], 1.0)
        self.assertAlmostEqual(second["average_power_w"], 10.0)
        self.assertAlmostEqual(result["discharge_energy_wh"], 30.0 / 3600.0)
        self.assertEqual(result["charge_energy_wh"], 0.0)
        self.assertAlmostEqual(result["net_energy_wh"], 30.0 / 3600.0)

    def test_trapezoid_integration_splits_at_window_boundaries(self) -> None:
        payload = {
            "samples": [sample(0, 0.0), sample(3, 3.0)],
            "bucket_duration_s": 1.0,
            "max_gap_s": 10.0,
        }
        result = self.service.aggregate_telemetry(payload)
        # Power ramps 0 -> 30 W linearly; window energies are 5, 15, 25 W*s.
        energies = [5.0, 15.0, 25.0]
        averages = [5.0, 15.0, 25.0]
        self.assertEqual(len(result["buckets"]), 3)
        for bucket, energy, average in zip(result["buckets"], energies, averages):
            self.assertEqual(bucket["covered_duration_s"], 1.0)
            self.assertAlmostEqual(
                bucket["discharge_energy_wh"], energy / 3600.0
            )
            self.assertAlmostEqual(bucket["average_power_w"], average)
        self.assertAlmostEqual(result["discharge_energy_wh"], 45.0 / 3600.0)

    def test_zero_crossing_splits_charge_and_discharge(self) -> None:
        payload = {
            "samples": [sample(0, 1.0), sample(10, -1.0)],
            "bucket_duration_s": 10.0,
            "max_gap_s": 20.0,
        }
        result = self.service.aggregate_telemetry(payload)
        self.assertEqual(len(result["buckets"]), 1)
        bucket = result["buckets"][0]
        # Power crosses zero at t=5: two triangles of 25 W*s each.
        self.assertAlmostEqual(bucket["discharge_energy_wh"], 25.0 / 3600.0)
        self.assertAlmostEqual(bucket["charge_energy_wh"], 25.0 / 3600.0)
        self.assertAlmostEqual(bucket["net_energy_wh"], 0.0)
        self.assertAlmostEqual(bucket["average_power_w"], 0.0)
        self.assertAlmostEqual(result["discharge_energy_wh"], 25.0 / 3600.0)
        self.assertAlmostEqual(result["charge_energy_wh"], 25.0 / 3600.0)
        self.assertAlmostEqual(result["net_energy_wh"], 0.0)

    def test_zero_crossing_combined_with_window_boundaries(self) -> None:
        payload = {
            "samples": [sample(0, 1.0), sample(10, -1.0)],
            "bucket_duration_s": 4.0,
            "max_gap_s": 20.0,
        }
        result = self.service.aggregate_telemetry(payload)
        # Power: 10 W at t=0, -10 W at t=10, zero at t=5.
        first, second, third = result["buckets"]
        self.assertAlmostEqual(first["discharge_energy_wh"], 24.0 / 3600.0)
        self.assertEqual(first["charge_energy_wh"], 0.0)
        self.assertAlmostEqual(second["discharge_energy_wh"], 1.0 / 3600.0)
        self.assertAlmostEqual(second["charge_energy_wh"], 9.0 / 3600.0)
        self.assertAlmostEqual(second["average_power_w"], -2.0)
        self.assertEqual(third["discharge_energy_wh"], 0.0)
        self.assertAlmostEqual(third["charge_energy_wh"], 16.0 / 3600.0)
        self.assertAlmostEqual(third["average_power_w"], -8.0)
        self.assertEqual(third["bucket_end_s"], 10.0)

    def test_gap_longer_than_max_gap_is_skipped(self) -> None:
        payload = {
            "samples": [sample(0), sample(1), sample(100), sample(101)],
            "bucket_duration_s": 1.0,
            "max_gap_s": 10.0,
        }
        result = self.service.aggregate_telemetry(payload)
        self.assertEqual(result["skipped_gap_count"], 1)
        self.assertEqual(len(result["buckets"]), 2)
        self.assertEqual(result["buckets"][0]["bucket_start_s"], 0.0)
        self.assertEqual(result["buckets"][1]["bucket_start_s"], 100.0)
        for bucket in result["buckets"]:
            self.assertEqual(bucket["covered_duration_s"], 1.0)
            self.assertAlmostEqual(bucket["average_power_w"], 10.0)
        self.assertAlmostEqual(result["discharge_energy_wh"], 20.0 / 3600.0)

    def test_gap_equal_to_max_gap_is_integrated(self) -> None:
        payload = {
            "samples": [sample(0), sample(10)],
            "bucket_duration_s": 5.0,
            "max_gap_s": 10.0,
        }
        result = self.service.aggregate_telemetry(payload)
        self.assertEqual(result["skipped_gap_count"], 0)
        self.assertEqual(len(result["buckets"]), 2)
        self.assertAlmostEqual(result["discharge_energy_wh"], 100.0 / 3600.0)

    def test_windows_without_coverage_are_omitted(self) -> None:
        payload = {
            "samples": [sample(0), sample(1), sample(10), sample(11)],
            "bucket_duration_s": 1.0,
            "max_gap_s": 2.0,
        }
        result = self.service.aggregate_telemetry(payload)
        self.assertEqual(result["skipped_gap_count"], 1)
        starts = [bucket["bucket_start_s"] for bucket in result["buckets"]]
        self.assertEqual(starts, [0.0, 10.0])

    def test_negative_current_counts_as_charge(self) -> None:
        payload = {
            "samples": [sample(0, -2.0), sample(2, -2.0)],
            "bucket_duration_s": 10.0,
            "max_gap_s": 5.0,
        }
        result = self.service.aggregate_telemetry(payload)
        self.assertEqual(result["discharge_energy_wh"], 0.0)
        self.assertAlmostEqual(result["charge_energy_wh"], 40.0 / 3600.0)
        self.assertAlmostEqual(result["net_energy_wh"], -40.0 / 3600.0)
        bucket = result["buckets"][0]
        self.assertAlmostEqual(bucket["average_power_w"], -20.0)

    def test_single_bucket_gives_insufficient_trend(self) -> None:
        payload = {
            "samples": [sample(0), sample(5)],
            "bucket_duration_s": 10.0,
            "max_gap_s": 10.0,
        }
        result = self.service.aggregate_telemetry(payload)
        self.assertIsNone(result["trend_slope_w_per_hour"])
        self.assertEqual(result["trend_status"], "insufficient")

    def test_flat_power_gives_stable_trend(self) -> None:
        result = self.service.aggregate_telemetry(base_request())
        self.assertAlmostEqual(result["trend_slope_w_per_hour"], 0.0)
        self.assertEqual(result["trend_status"], "stable")

    def test_rising_power_gives_increasing_trend(self) -> None:
        payload = {
            "samples": [sample(0, 0.0), sample(3, 3.0)],
            "bucket_duration_s": 1.0,
            "max_gap_s": 10.0,
        }
        result = self.service.aggregate_telemetry(payload)
        # Window average powers 5, 15, 25 W at midpoints 0.5, 1.5, 2.5 s.
        self.assertAlmostEqual(result["trend_slope_w_per_hour"], 36000.0)
        self.assertEqual(result["trend_status"], "increasing")

    def test_falling_power_gives_decreasing_trend(self) -> None:
        payload = {
            "samples": [sample(0, 3.0), sample(3, 0.0)],
            "bucket_duration_s": 1.0,
            "max_gap_s": 10.0,
        }
        result = self.service.aggregate_telemetry(payload)
        self.assertAlmostEqual(result["trend_slope_w_per_hour"], -36000.0)
        self.assertEqual(result["trend_status"], "decreasing")

    def test_trend_threshold_bounds_classification(self) -> None:
        payload = {
            "samples": [sample(0, 0.0), sample(3, 3.0)],
            "bucket_duration_s": 1.0,
            "max_gap_s": 10.0,
            "trend_threshold_w_per_hour": 36000.0,
        }
        result = self.service.aggregate_telemetry(payload)
        # Slope exactly at the threshold is stable, not increasing.
        self.assertAlmostEqual(result["trend_slope_w_per_hour"], 36000.0)
        self.assertEqual(result["trend_status"], "stable")

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.aggregate_telemetry(payload)
        self.assertEqual(payload, snapshot)


class TelemetryAggregateValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.aggregate_telemetry(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_samples_rules(self) -> None:
        self.assert_code({}, "invalid_samples")
        self.assert_code({"samples": []}, "invalid_samples")
        self.assert_code({"samples": None}, "invalid_samples")
        self.assert_code({"samples": "x"}, "invalid_samples")
        self.assert_code({"samples": [sample(0)]}, "invalid_samples")
        self.assert_code({"samples": [1, 2]}, "invalid_samples")
        self.assert_code({"samples": [sample(0), "x"]}, "invalid_samples")

    def test_timestamp_rules(self) -> None:
        self.assert_code(
            {"samples": [{"current_a": 1.0, "voltage_v": 10.0}, sample(1)]},
            "invalid_timestamp",
        )
        self.assert_code(
            {"samples": [sample(float("nan")), sample(1)]}, "invalid_timestamp"
        )
        self.assert_code({"samples": [sample(True), sample(1)]}, "invalid_timestamp")
        self.assert_code({"samples": [sample("0"), sample(1)]}, "invalid_timestamp")
        self.assert_code(
            {"samples": [sample(1), sample(1)]}, "invalid_timestamp"
        )
        self.assert_code(
            {"samples": [sample(2), sample(1)]}, "invalid_timestamp"
        )

    def test_measurement_rules(self) -> None:
        self.assert_code(
            {"samples": [{"timestamp_s": 0, "voltage_v": 10.0}, sample(1)]},
            "invalid_measurement",
        )
        self.assert_code(
            {"samples": [{"timestamp_s": 0, "current_a": 1.0}, sample(1)]},
            "invalid_measurement",
        )
        self.assert_code(
            {"samples": [sample(0, current=True), sample(1)]},
            "invalid_measurement",
        )
        self.assert_code(
            {"samples": [sample(0, voltage=False), sample(1)]},
            "invalid_measurement",
        )
        self.assert_code(
            {"samples": [sample(0, current=float("inf")), sample(1)]},
            "invalid_measurement",
        )

    def test_bucket_duration_rules(self) -> None:
        for bad in (None, 0, -1.0, True, "2", float("nan"), float("inf")):
            self.assert_code(
                {"samples": [sample(0), sample(1)], "max_gap_s": 5,
                 "bucket_duration_s": bad},
                "invalid_options",
            )
        self.assert_code(
            {"samples": [sample(0), sample(1)], "max_gap_s": 5},
            "invalid_options",
        )

    def test_max_gap_rules(self) -> None:
        for bad in (None, 0, -1.0, True, "5", float("nan"), float("inf")):
            self.assert_code(
                {"samples": [sample(0), sample(1)], "bucket_duration_s": 2,
                 "max_gap_s": bad},
                "invalid_options",
            )
        self.assert_code(
            {"samples": [sample(0), sample(1)], "bucket_duration_s": 2},
            "invalid_options",
        )

    def test_trend_threshold_rules(self) -> None:
        for bad in (None, -0.1, True, "0", float("nan"), float("inf")):
            self.assert_code(
                base_request(trend_threshold_w_per_hour=bad),
                "invalid_options",
            )

    def test_trend_threshold_defaults_to_zero(self) -> None:
        result = self.service.aggregate_telemetry(base_request())
        self.assertEqual(result["trend_status"], "stable")

    def test_trend_threshold_accepts_zero_and_positive(self) -> None:
        for threshold in (0, 0.0, 12.5):
            result = self.service.aggregate_telemetry(
                base_request(trend_threshold_w_per_hour=threshold)
            )
            self.assertEqual(result["trend_status"], "stable")


class TelemetryAggregateHttpTest(unittest.TestCase):
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

    def test_aggregate_route_returns_200(self) -> None:
        status, body = self.post(
            TELEMETRY_AGGREGATE_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["buckets"]), 2)
        self.assertEqual(body["skipped_gap_count"], 0)
        self.assertEqual(body["trend_status"], "stable")

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_AGGREGATE_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_AGGREGATE_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_AGGREGATE_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_samples_is_422(self) -> None:
        status, body = self.post(
            TELEMETRY_AGGREGATE_ROUTE,
            json.dumps({"samples": [sample(0)]}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_invalid_options_is_422(self) -> None:
        status, body = self.post(
            TELEMETRY_AGGREGATE_ROUTE,
            json.dumps({"samples": [sample(0), sample(1)], "max_gap_s": 5}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_options")

    def test_unknown_post_path_is_404(self) -> None:
        status, body = self.post("/v1/telemetry/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_existing_filter_route_still_works(self) -> None:
        status, body = self.post(
            TELEMETRY_FILTER_ROUTE,
            json.dumps({"samples": [sample(0), sample(1)]}).encode(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["sample_count"], 2)


if __name__ == "__main__":
    unittest.main()
