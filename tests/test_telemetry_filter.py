import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import TELEMETRY_FILTER_ROUTE, Handler
from powerrail.service import ApiError, Service


def base_request(**overrides):
    payload = {
        "samples": [
            {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 12.0},
            {"timestamp_s": 1, "current_a": 1.2, "voltage_v": 12.2},
            {"timestamp_s": 2, "current_a": 8.0, "voltage_v": 20.0},
            {"timestamp_s": 3, "current_a": 1.1, "voltage_v": 12.1},
        ]
    }
    payload.update(overrides)
    return payload


class TelemetryFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_response_shape_and_counts_single_segment(self) -> None:
        result = self.service.filter_telemetry(base_request())
        self.assertEqual(result["sample_count"], 4)
        self.assertEqual(result["segment_count"], 1)
        self.assertEqual(len(result["samples"]), 4)
        self.assertEqual(
            [item["timestamp_s"] for item in result["samples"]], [0, 1, 2, 3]
        )
        self.assertTrue(result["samples"][0]["segment_start"])
        self.assertFalse(result["samples"][1]["segment_start"])
        self.assertFalse(result["samples"][2]["segment_start"])
        self.assertFalse(result["samples"][3]["segment_start"])

    def test_first_sample_adopts_median_directly(self) -> None:
        result = self.service.filter_telemetry(base_request())
        first = result["samples"][0]
        self.assertEqual(first["filtered_current_a"], 1.0)
        self.assertEqual(first["filtered_voltage_v"], 12.0)

    def test_median_then_ema_blend(self) -> None:
        # window=3, smoothing=0.5 for hand-computable values.
        payload = base_request(
            median_window=3,
            smoothing_factor=0.5,
            samples=[
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 10.0},
                {"timestamp_s": 1, "current_a": 2.0, "voltage_v": 20.0},
                {"timestamp_s": 2, "current_a": 3.0, "voltage_v": 30.0},
                {"timestamp_s": 3, "current_a": 100.0, "voltage_v": 40.0},
            ],
        )
        result = self.service.filter_telemetry(payload)["samples"]
        # idx0: median(1)=1, idx1: median(1,2)=1.5 -> 0.5*1.5+0.5*1=1.25
        # idx2: median(1,2,3)=2 -> 0.5*2+0.5*1.25=1.625
        # idx3: median(2,3,100)=3 -> 0.5*3+0.5*1.625=2.3125 (spike rejected)
        self.assertEqual(result[0]["filtered_current_a"], 1.0)
        self.assertAlmostEqual(result[1]["filtered_current_a"], 1.25)
        self.assertAlmostEqual(result[2]["filtered_current_a"], 1.625)
        self.assertAlmostEqual(result[3]["filtered_current_a"], 2.3125)
        self.assertAlmostEqual(result[1]["filtered_voltage_v"], 12.5)

    def test_even_candidate_count_averages_middle_pair(self) -> None:
        payload = base_request(
            median_window=3,
            smoothing_factor=1.0,
            samples=[
                {"timestamp_s": 0, "current_a": 4.0, "voltage_v": 0.0},
                {"timestamp_s": 1, "current_a": 10.0, "voltage_v": 0.0},
            ],
        )
        result = self.service.filter_telemetry(payload)["samples"]
        # median of two candidates = (4 + 10) / 2; smoothing 1 means passthrough
        self.assertEqual(result[1]["filtered_current_a"], 7.0)

    def test_smoothing_factor_one_is_passthrough_of_median(self) -> None:
        payload = base_request(
            smoothing_factor=1.0,
            samples=[
                {"timestamp_s": 0, "current_a": 5.0, "voltage_v": 1.0},
                {"timestamp_s": 1, "current_a": 1.0, "voltage_v": 2.0},
                {"timestamp_s": 2, "current_a": 3.0, "voltage_v": 3.0},
            ],
        )
        result = self.service.filter_telemetry(payload)["samples"]
        self.assertEqual(result[2]["filtered_current_a"], 3.0)

    def test_window_one_skips_median_filtering(self) -> None:
        payload = base_request(
            median_window=1,
            smoothing_factor=1.0,
            samples=[
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 1.0},
                {"timestamp_s": 1, "current_a": 99.0, "voltage_v": 2.0},
            ],
        )
        result = self.service.filter_telemetry(payload)["samples"]
        self.assertEqual(result[1]["filtered_current_a"], 99.0)

    def test_reset_gap_starts_new_segment_for_both_channels(self) -> None:
        payload = base_request(
            reset_gap_s=9.0,
            samples=[
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 10.0},
                {"timestamp_s": 5, "current_a": 2.0, "voltage_v": 20.0},
                {"timestamp_s": 15, "current_a": 50.0, "voltage_v": 50.0},
                {"timestamp_s": 16, "current_a": 50.0, "voltage_v": 50.0},
            ],
        )
        result = self.service.filter_telemetry(payload)
        self.assertEqual(result["segment_count"], 2)
        samples = result["samples"]
        self.assertTrue(samples[2]["segment_start"])
        self.assertTrue(samples[0]["segment_start"])
        self.assertFalse(samples[1]["segment_start"])
        # segment restart adopts the raw (median) value, no EMA carryover
        self.assertEqual(samples[2]["filtered_current_a"], 50.0)
        self.assertEqual(samples[2]["filtered_voltage_v"], 50.0)

    def test_gap_equal_to_reset_gap_does_not_reset(self) -> None:
        payload = base_request(
            reset_gap_s=10.0,
            samples=[
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 10.0},
                {"timestamp_s": 10, "current_a": 50.0, "voltage_v": 50.0},
            ],
        )
        result = self.service.filter_telemetry(payload)
        self.assertEqual(result["segment_count"], 1)
        self.assertFalse(result["samples"][1]["segment_start"])

    def test_multiple_segments_clear_median_history(self) -> None:
        payload = base_request(
            median_window=3,
            smoothing_factor=1.0,
            reset_gap_s=5.0,
            samples=[
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 1.0},
                {"timestamp_s": 1, "current_a": 2.0, "voltage_v": 2.0},
                {"timestamp_s": 2, "current_a": 3.0, "voltage_v": 3.0},
                {"timestamp_s": 10, "current_a": 9.0, "voltage_v": 9.0},
            ],
        )
        samples = self.service.filter_telemetry(payload)["samples"]
        self.assertEqual(samples[3]["filtered_current_a"], 9.0)
        self.assertEqual(samples[3]["filtered_voltage_v"], 9.0)

    def test_defaults_match_documentation(self) -> None:
        payload = base_request()
        result_default = self.service.filter_telemetry(payload)
        result_explicit = self.service.filter_telemetry(
            base_request(median_window=3, smoothing_factor=0.25, reset_gap_s=30.0)
        )
        self.assertEqual(result_default, result_explicit)

    def test_input_is_not_modified(self) -> None:
        payload = base_request()
        snapshot = copy.deepcopy(payload)
        self.service.filter_telemetry(payload)
        self.assertEqual(payload, snapshot)

    def test_integer_timestamp_is_preserved(self) -> None:
        result = self.service.filter_telemetry(base_request())
        self.assertIsInstance(result["samples"][0]["timestamp_s"], int)


class TelemetryFilterValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.filter_telemetry(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_samples_rules(self) -> None:
        self.assert_code({}, "invalid_samples")
        self.assert_code({"samples": []}, "invalid_samples")
        self.assert_code({"samples": "x"}, "invalid_samples")
        self.assert_code({"samples": [1]}, "invalid_samples")

    def test_timestamp_rules(self) -> None:
        valid = {"current_a": 1.0, "voltage_v": 12.0}
        self.assert_code(
            {"samples": [{"current_a": 1.0, "voltage_v": 12.0}]}, "invalid_timestamp"
        )
        self.assert_code(
            {"samples": [{"timestamp_s": True, **valid}]}, "invalid_timestamp"
        )
        self.assert_code(
            {"samples": [{"timestamp_s": float("nan"), **valid}]},
            "invalid_timestamp",
        )
        self.assert_code(
            {
                "samples": [
                    {"timestamp_s": 1, **valid},
                    {"timestamp_s": 1, **valid},
                ]
            },
            "invalid_timestamp",
        )
        self.assert_code(
            {
                "samples": [
                    {"timestamp_s": 2, **valid},
                    {"timestamp_s": 1, **valid},
                ]
            },
            "invalid_timestamp",
        )

    def test_measurement_rules(self) -> None:
        for key, value in (("current_a", None), ("voltage_v", None)):
            self.assert_code(
                {
                    "samples": [
                        {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 12.0, key: value}
                    ]
                },
                "invalid_measurement",
            )
        self.assert_code(
            {
                "samples": [
                    {"timestamp_s": 0, "current_a": True, "voltage_v": 12.0}
                ]
            },
            "invalid_measurement",
        )
        self.assert_code(
            {
                "samples": [
                    {"timestamp_s": 0, "current_a": 1.0, "voltage_v": float("inf")}
                ]
            },
            "invalid_measurement",
        )

    def test_median_window_rules(self) -> None:
        for bad in (0, 2, 4, 12, 1.5, "3", True, None, -1):
            self.assert_code(base_request(median_window=bad), "invalid_filter_options")

    def test_smoothing_factor_rules(self) -> None:
        for bad in (0.0, -0.1, 1.01, "0.25", True, float("inf")):
            self.assert_code(
                base_request(smoothing_factor=bad), "invalid_filter_options"
            )

    def test_reset_gap_rules(self) -> None:
        for bad in (0.0, -1.0, "30", True, float("nan")):
            self.assert_code(base_request(reset_gap_s=bad), "invalid_filter_options")


class TelemetryFilterHttpTest(unittest.TestCase):
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

    def test_filter_route_returns_200(self) -> None:
        status, body = self.post(
            TELEMETRY_FILTER_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["sample_count"], 4)
        self.assertEqual(body["segment_count"], 1)
        self.assertEqual(len(body["samples"]), 4)
        self.assertTrue(body["samples"][0]["segment_start"])

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_FILTER_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_FILTER_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_FILTER_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_bad_samples_is_422(self) -> None:
        status, body = self.post(
            TELEMETRY_FILTER_ROUTE, json.dumps({"samples": []}).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_bad_filter_options_is_422(self) -> None:
        payload = base_request(median_window=2)
        status, body = self.post(
            TELEMETRY_FILTER_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_filter_options")

    def test_unknown_path_still_404(self) -> None:
        status, body = self.post("/v1/telemetry/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
