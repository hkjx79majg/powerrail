import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import (
    TELEMETRY_AGGREGATE_ROUTE,
    TELEMETRY_FILTER_ROUTE,
    Handler,
)
from powerrail.service import ApiError, Service


def sample(timestamp, current=1.0, voltage=10.0):
    return {"timestamp_s": timestamp, "current_a": current, "voltage_v": voltage}


def request(samples, bucket=10.0, gap=60.0, **overrides):
    payload = {
        "samples": samples,
        "bucket_duration_s": bucket,
        "max_gap_s": gap,
    }
    payload.update(overrides)
    return payload


class TelemetryAggregateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def aggregate(self, *args, **kwargs):
        return self.service.aggregate_telemetry(request(*args, **kwargs))

    def test_top_level_and_bucket_shape(self) -> None:
        result = self.aggregate(
            [sample(0, 1.0, 10.0), sample(10, 2.0, 10.0), sample(20, 1.0, 10.0)]
        )
        self.assertEqual(
            set(result),
            {
                "buckets",
                "discharge_energy_wh",
                "charge_energy_wh",
                "net_energy_wh",
                "skipped_gap_count",
                "trend_slope_w_per_hour",
                "trend_status",
            },
        )
        self.assertEqual(len(result["buckets"]), 2)
        for bucket in result["buckets"]:
            self.assertEqual(
                set(bucket),
                {
                    "start_time_s",
                    "end_time_s",
                    "covered_duration_s",
                    "average_power_w",
                    "discharge_energy_wh",
                    "charge_energy_wh",
                    "net_energy_wh",
                },
            )

    def test_constant_power_single_bucket(self) -> None:
        result = self.aggregate([sample(0, 1.0, 10.0), sample(10, 1.0, 10.0)])
        bucket = result["buckets"][0]
        self.assertEqual(bucket["start_time_s"], 0.0)
        self.assertEqual(bucket["end_time_s"], 10.0)
        self.assertEqual(bucket["covered_duration_s"], 10.0)
        self.assertAlmostEqual(bucket["discharge_energy_wh"], 100.0 / 3600.0)
        self.assertEqual(bucket["charge_energy_wh"], 0.0)
        self.assertAlmostEqual(bucket["net_energy_wh"], 100.0 / 3600.0)
        self.assertAlmostEqual(bucket["average_power_w"], 10.0)

    def test_interval_split_at_bucket_boundary(self) -> None:
        # Power rises linearly 10 -> 20 across a bucket boundary at t=10.
        result = self.aggregate([sample(0, 1.0, 10.0), sample(20, 2.0, 10.0)])
        self.assertEqual(len(result["buckets"]), 2)
        first, second = result["buckets"]
        self.assertEqual(first["start_time_s"], 0.0)
        self.assertEqual(second["end_time_s"], 20.0)
        # Power at the boundary is 15 W: halves integrate 12.5 W and 17.5 W.
        self.assertAlmostEqual(first["average_power_w"], 12.5)
        self.assertAlmostEqual(second["average_power_w"], 17.5)
        self.assertAlmostEqual(first["covered_duration_s"], 10.0)
        self.assertAlmostEqual(second["covered_duration_s"], 10.0)
        self.assertAlmostEqual(
            result["discharge_energy_wh"], (12.5 * 10 + 17.5 * 10) / 3600.0
        )
        self.assertAlmostEqual(
            result["net_energy_wh"], (10.0 + 20.0) / 2.0 * 20.0 / 3600.0
        )

    def test_zero_crossing_splits_discharge_and_charge(self) -> None:
        # Power crosses zero exactly at t=5; naive trapezoid would net to 0
        # with neither discharge nor charge recorded.
        result = self.aggregate([sample(0, 1.0, 10.0), sample(10, -1.0, 10.0)])
        self.assertEqual(len(result["buckets"]), 1)
        bucket = result["buckets"][0]
        self.assertAlmostEqual(bucket["discharge_energy_wh"], 25.0 / 3600.0)
        self.assertAlmostEqual(bucket["charge_energy_wh"], 25.0 / 3600.0)
        self.assertAlmostEqual(bucket["net_energy_wh"], 0.0)
        self.assertAlmostEqual(bucket["average_power_w"], 0.0)
        self.assertEqual(bucket["covered_duration_s"], 10.0)

    def test_negative_current_charges(self) -> None:
        result = self.aggregate([sample(0, -2.0, 10.0), sample(10, -2.0, 10.0)])
        bucket = result["buckets"][0]
        self.assertAlmostEqual(bucket["charge_energy_wh"], 200.0 / 3600.0)
        self.assertEqual(bucket["discharge_energy_wh"], 0.0)
        self.assertAlmostEqual(bucket["average_power_w"], -20.0)
        self.assertAlmostEqual(result["charge_energy_wh"], 200.0 / 3600.0)
        self.assertAlmostEqual(result["discharge_energy_wh"], 0.0)
        self.assertAlmostEqual(result["net_energy_wh"], -200.0 / 3600.0)

    def test_buckets_are_aligned_to_first_timestamp(self) -> None:
        result = self.aggregate([sample(100, 1.0, 10.0), sample(110, 1.0, 10.0)])
        bucket = result["buckets"][0]
        self.assertEqual(bucket["start_time_s"], 100.0)
        self.assertEqual(bucket["end_time_s"], 110.0)

    def test_gap_longer_than_max_is_skipped_and_counted(self) -> None:
        samples = [
            sample(0, 1.0, 10.0),
            sample(10, 1.0, 10.0),
            sample(100, 1.0, 10.0),
            sample(110, 1.0, 10.0),
        ]
        result = self.aggregate(samples, bucket=10.0, gap=60.0)
        self.assertEqual(result["skipped_gap_count"], 1)
        # Interior buckets covered only by the skipped gap are omitted.
        self.assertEqual(
            [(b["start_time_s"], b["end_time_s"]) for b in result["buckets"]],
            [(0.0, 10.0), (100.0, 110.0)],
        )
        for bucket in result["buckets"]:
            self.assertEqual(bucket["covered_duration_s"], 10.0)
            self.assertAlmostEqual(bucket["discharge_energy_wh"], 100.0 / 3600.0)
        self.assertAlmostEqual(result["discharge_energy_wh"], 200.0 / 3600.0)

    def test_gap_equal_to_max_is_integrated(self) -> None:
        result = self.aggregate(
            [sample(0, 1.0, 10.0), sample(10, 1.0, 10.0)], bucket=10.0, gap=10.0
        )
        self.assertEqual(result["skipped_gap_count"], 0)
        self.assertEqual(len(result["buckets"]), 1)
        self.assertEqual(result["buckets"][0]["covered_duration_s"], 10.0)

    def test_buckets_without_coverage_are_omitted(self) -> None:
        samples = [
            sample(0, 1.0, 10.0),
            sample(30, 1.0, 10.0),
            sample(300, 1.0, 10.0),
            sample(330, 1.0, 10.0),
        ]
        result = self.aggregate(samples, bucket=10.0, gap=60.0)
        self.assertEqual(result["skipped_gap_count"], 1)
        self.assertEqual(len(result["buckets"]), 6)
        self.assertEqual(
            [b["start_time_s"] for b in result["buckets"]],
            [0.0, 10.0, 20.0, 300.0, 310.0, 320.0],
        )

    def test_trend_increasing(self) -> None:
        # Bucket averages are 10 W and 15 W at midpoints 5s and 15s.
        result = self.aggregate(
            [sample(0, 1.0, 10.0), sample(10, 1.0, 10.0), sample(20, 2.0, 10.0)]
        )
        self.assertAlmostEqual(result["trend_slope_w_per_hour"], 1800.0)
        self.assertEqual(result["trend_status"], "increasing")

    def test_trend_decreasing(self) -> None:
        # Bucket averages are 15 W and 5 W.
        result = self.aggregate(
            [sample(0, 2.0, 10.0), sample(10, 1.0, 10.0), sample(20, 0.0, 10.0)]
        )
        self.assertAlmostEqual(result["trend_slope_w_per_hour"], -3600.0)
        self.assertEqual(result["trend_status"], "decreasing")

    def test_trend_stable_within_threshold(self) -> None:
        result = self.aggregate(
            [sample(0, 1.0, 10.0), sample(10, 1.0, 10.0), sample(20, 2.0, 10.0)],
            trend_threshold_w_per_hour=2000.0,
        )
        self.assertAlmostEqual(result["trend_slope_w_per_hour"], 1800.0)
        self.assertEqual(result["trend_status"], "stable")

    def test_trend_negative_threshold_margin(self) -> None:
        # Slope -3600 with threshold 4000 is within +/- threshold -> stable.
        result = self.aggregate(
            [sample(0, 2.0, 10.0), sample(10, 1.0, 10.0), sample(20, 0.0, 10.0)],
            trend_threshold_w_per_hour=4000.0,
        )
        self.assertEqual(result["trend_status"], "stable")

    def test_trend_constant_power_is_stable(self) -> None:
        result = self.aggregate(
            [sample(i, 1.0, 10.0) for i in range(0, 31, 10)],
            trend_threshold_w_per_hour=5.0,
        )
        self.assertEqual(result["trend_slope_w_per_hour"], 0.0)
        self.assertEqual(result["trend_status"], "stable")

    def test_trend_insufficient_with_single_bucket(self) -> None:
        result = self.aggregate([sample(0, 1.0, 10.0), sample(10, 1.0, 10.0)])
        self.assertIsNone(result["trend_slope_w_per_hour"])
        self.assertEqual(result["trend_status"], "insufficient")

    def test_default_trend_threshold_is_zero(self) -> None:
        result = self.aggregate(
            [sample(0, 1.0, 10.0), sample(10, 1.0, 10.0), sample(20, 0.0, 10.0)]
        )
        # Any negative slope is decreasing under the zero default.
        self.assertEqual(result["trend_status"], "decreasing")

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = request(
            [sample(0, 1.0, 10.0), sample(10, -1.0, 10.0), sample(20, 1.0, 10.0)]
        )
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

    def valid_body(self, **overrides):
        payload = {
            "samples": [sample(0), sample(10)],
            "bucket_duration_s": 10.0,
            "max_gap_s": 60.0,
        }
        payload.update(overrides)
        return payload

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_samples_rules(self) -> None:
        self.assert_code({"bucket_duration_s": 1, "max_gap_s": 1}, "invalid_samples")
        self.assert_code(self.valid_body(samples=[]), "invalid_samples")
        self.assert_code(self.valid_body(samples=[sample(0)]), "invalid_samples")
        self.assert_code(self.valid_body(samples=None), "invalid_samples")
        self.assert_code(self.valid_body(samples="x"), "invalid_samples")
        self.assert_code(self.valid_body(samples=[1, 2]), "invalid_samples")
        self.assert_code(self.valid_body(samples=["x", {}]), "invalid_samples")

    def test_timestamp_rules(self) -> None:
        self.assert_code(
            self.valid_body(
                samples=[
                    {"current_a": 1.0, "voltage_v": 10.0},
                    sample(10),
                ]
            ),
            "invalid_timestamp",
        )
        self.assert_code(
            self.valid_body(samples=[sample(float("nan")), sample(10)]),
            "invalid_timestamp",
        )
        self.assert_code(
            self.valid_body(samples=[sample(True), sample(10)]),
            "invalid_timestamp",
        )
        self.assert_code(
            self.valid_body(samples=[sample("1"), sample(10)]),
            "invalid_timestamp",
        )
        self.assert_code(
            self.valid_body(samples=[sample(10), sample(10)]),
            "invalid_timestamp",
        )
        self.assert_code(
            self.valid_body(samples=[sample(11), sample(10)]),
            "invalid_timestamp",
        )

    def test_measurement_rules(self) -> None:
        self.assert_code(
            self.valid_body(
                samples=[
                    {"timestamp_s": 0, "voltage_v": 10.0},
                    sample(10),
                ]
            ),
            "invalid_measurement",
        )
        self.assert_code(
            self.valid_body(
                samples=[
                    {"timestamp_s": 0, "current_a": 1.0},
                    sample(10),
                ]
            ),
            "invalid_measurement",
        )
        self.assert_code(
            self.valid_body(samples=[sample(0, current=True), sample(10)]),
            "invalid_measurement",
        )
        self.assert_code(
            self.valid_body(samples=[sample(0, voltage=False), sample(10)]),
            "invalid_measurement",
        )
        self.assert_code(
            self.valid_body(samples=[sample(0, voltage=float("inf")), sample(10)]),
            "invalid_measurement",
        )

    def test_bucket_duration_rules(self) -> None:
        for bad in (None, 0, -1.0, True, "10", float("nan"), float("inf")):
            self.assert_code(self.valid_body(bucket_duration_s=bad), "invalid_options")

    def test_max_gap_rules(self) -> None:
        for bad in (None, 0, -1.0, True, "60", float("nan"), float("inf")):
            self.assert_code(self.valid_body(max_gap_s=bad), "invalid_options")

    def test_trend_threshold_rules(self) -> None:
        for bad in (True, "0", -0.1, float("nan"), float("inf")):
            self.assert_code(
                self.valid_body(trend_threshold_w_per_hour=bad),
                "invalid_options",
            )

    def test_trend_threshold_zero_is_allowed(self) -> None:
        result = self.service.aggregate_telemetry(
            self.valid_body(trend_threshold_w_per_hour=0)
        )
        self.assertEqual(result["trend_status"], "insufficient")


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

    def valid_body(self):
        return {
            "samples": [
                sample(0, 1.0, 10.0),
                sample(10, 1.0, 10.0),
                sample(20, 2.0, 10.0),
            ],
            "bucket_duration_s": 10,
            "max_gap_s": 60,
        }

    def test_aggregate_route_returns_200(self) -> None:
        status, body = self.post(
            TELEMETRY_AGGREGATE_ROUTE, json.dumps(self.valid_body()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["buckets"]), 2)
        self.assertEqual(body["skipped_gap_count"], 0)
        self.assertEqual(body["trend_status"], "increasing")
        self.assertAlmostEqual(body["trend_slope_w_per_hour"], 1800.0)

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
            json.dumps({"samples": [], "bucket_duration_s": 1, "max_gap_s": 1}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_invalid_timestamp_is_422(self) -> None:
        payload = self.valid_body()
        payload["samples"][1]["timestamp_s"] = payload["samples"][0]["timestamp_s"]
        status, body = self.post(
            TELEMETRY_AGGREGATE_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_timestamp")

    def test_invalid_measurement_is_422(self) -> None:
        payload = self.valid_body()
        payload["samples"][0]["voltage_v"] = "10"
        status, body = self.post(
            TELEMETRY_AGGREGATE_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_measurement")

    def test_invalid_options_is_422(self) -> None:
        payload = self.valid_body()
        payload["bucket_duration_s"] = 0
        status, body = self.post(
            TELEMETRY_AGGREGATE_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_options")

    def test_filter_route_still_works(self) -> None:
        status, body = self.post(
            TELEMETRY_FILTER_ROUTE,
            json.dumps({"samples": [sample(0), sample(1)]}).encode(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["sample_count"], 2)


if __name__ == "__main__":
    unittest.main()
