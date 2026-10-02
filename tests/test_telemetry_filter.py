import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import HEALTH_ROUTE, TELEMETRY_FILTER_ROUTE, Handler
from powerrail.service import ApiError, Service


def sample(timestamp, current=1.0, voltage=10.0):
    return {"timestamp_s": timestamp, "current_a": current, "voltage_v": voltage}


def base_request(**overrides):
    payload = {
        "samples": [
            sample(0, 1.0, 10.0),
            sample(1, 2.0, 10.2),
            sample(2, 1.5, 10.1),
        ]
    }
    payload.update(overrides)
    return payload


class TelemetryFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_response_shape_matches_input_length_and_order(self) -> None:
        payload = base_request()
        result = self.service.filter_telemetry(payload)
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["segment_count"], 1)
        self.assertEqual([item["timestamp_s"] for item in result["samples"]], [0, 1, 2])
        for item in result["samples"]:
            self.assertEqual(
                set(item),
                {
                    "timestamp_s",
                    "filtered_current_a",
                    "filtered_voltage_v",
                    "segment_start",
                },
            )
            self.assertIsInstance(item["segment_start"], bool)
        self.assertEqual(
            [item["segment_start"] for item in result["samples"]], [True, False, False]
        )

    def test_first_segment_item_adopts_median_directly(self) -> None:
        result = self.service.filter_telemetry(base_request())
        first = result["samples"][0]
        self.assertEqual(first["filtered_current_a"], 1.0)
        self.assertEqual(first["filtered_voltage_v"], 10.0)

    def test_even_candidate_count_averages_middle_pair(self) -> None:
        # Second item has two candidates -> median is the mean of the pair.
        result = self.service.filter_telemetry(base_request())
        # current median([1, 2]) = 1.5; EMA: 0.25*1.5 + 0.75*1 = 1.125
        self.assertAlmostEqual(result["samples"][1]["filtered_current_a"], 1.125)
        # voltage median([10, 10.2]) = 10.1; 0.25*10.1 + 0.75*10 = 10.025
        self.assertAlmostEqual(result["samples"][1]["filtered_voltage_v"], 10.025)

    def test_median_window_suppresses_spikes(self) -> None:
        payload = {
            "median_window": 3,
            "smoothing_factor": 1.0,
            "samples": [
                sample(0, 1.0),
                sample(1, 100.0),
                sample(2, 1.0),
                sample(3, 1.0),
            ],
        }
        values = [
            item["filtered_current_a"]
            for item in self.service.filter_telemetry(payload)["samples"]
        ]
        # Window at the spike (2 candidates) averages 1 and 100, but at the
        # next sample the triple median rejects the spike entirely.
        self.assertEqual(values[2], 1.0)
        self.assertEqual(values[3], 1.0)

    def test_channels_are_filtered_independently(self) -> None:
        payload = {
            "median_window": 1,
            "smoothing_factor": 1.0,
            "samples": [sample(0, 3.0, 12.0), sample(1, 9.0, 6.0)],
        }
        result = self.service.filter_telemetry(payload)["samples"]
        self.assertEqual([i["filtered_current_a"] for i in result], [3.0, 9.0])
        self.assertEqual([i["filtered_voltage_v"] for i in result], [12.0, 6.0])

    def test_window_larger_than_history(self) -> None:
        payload = {
            "median_window": 11,
            "smoothing_factor": 1.0,
            "samples": [sample(i, float(i % 3), float(10 + i % 2)) for i in range(12)],
        }
        result = self.service.filter_telemetry(payload)["samples"]
        # 12th item sees only the 10 previous raw values (plus current).
        self.assertEqual(result[11]["filtered_current_a"], 1.0)

    def test_gap_greater_than_reset_starts_new_segment(self) -> None:
        payload = {"samples": [sample(0), sample(30), sample(31), sample(70)]}
        result = self.service.filter_telemetry(payload)
        self.assertEqual(result["segment_count"], 2)
        self.assertEqual(
            [item["segment_start"] for item in result["samples"]],
            [True, False, False, True],
        )
        # Reset item adopts its own median without blending with history.
        self.assertEqual(result["samples"][3]["filtered_current_a"], 1.0)

    def test_gap_equal_to_reset_does_not_reset(self) -> None:
        payload = {"reset_gap_s": 5, "samples": [sample(0, 1.0), sample(5, 8.0)]}
        result = self.service.filter_telemetry(payload)
        self.assertEqual(result["segment_count"], 1)
        self.assertEqual(
            [item["segment_start"] for item in result["samples"]], [True, False]
        )
        # median([1, 8]) = 4.5; 0.25*4.5 + 0.75*1 = 1.875
        self.assertAlmostEqual(result["samples"][1]["filtered_current_a"], 1.875)

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.filter_telemetry(payload)
        self.assertEqual(payload, snapshot)

    def test_timestamp_is_preserved_verbatim(self) -> None:
        payload = {"samples": [sample(7), sample(8)]}
        result = self.service.filter_telemetry(payload)
        self.assertEqual(result["samples"][0]["timestamp_s"], 7)
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
        self.assert_code({"samples": None}, "invalid_samples")
        self.assert_code({"samples": "x"}, "invalid_samples")
        self.assert_code({"samples": [1]}, "invalid_samples")
        self.assert_code({"samples": ["x"]}, "invalid_samples")

    def test_timestamp_rules(self) -> None:
        self.assert_code(
            {"samples": [{"current_a": 1.0, "voltage_v": 10.0}]}, "invalid_timestamp"
        )
        self.assert_code({"samples": [sample(float("nan"))]}, "invalid_timestamp")
        self.assert_code({"samples": [sample(True)]}, "invalid_timestamp")
        self.assert_code({"samples": [sample("1")]}, "invalid_timestamp")
        self.assert_code(
            {"samples": [sample(1), sample(1)]}, "invalid_timestamp"
        )
        self.assert_code(
            {"samples": [sample(2), sample(1)]}, "invalid_timestamp"
        )

    def test_measurement_rules(self) -> None:
        self.assert_code(
            {"samples": [{"timestamp_s": 0, "voltage_v": 10.0}]},
            "invalid_measurement",
        )
        self.assert_code(
            {"samples": [{"timestamp_s": 0, "current_a": 1.0}]},
            "invalid_measurement",
        )
        self.assert_code({"samples": [sample(0, current=True)]}, "invalid_measurement")
        self.assert_code({"samples": [sample(0, voltage=False)]}, "invalid_measurement")
        self.assert_code(
            {"samples": [sample(0, current=float("inf"))]}, "invalid_measurement"
        )

    def test_median_window_rules(self) -> None:
        for bad in (None, 0, 2, 4, 11 + 2, -1, 1.0, 3.0, True, "3"):
            self.assert_code(
                {"median_window": bad, "samples": [sample(0)]},
                "invalid_filter_options",
            )

    def test_median_window_accepts_all_odd_values_in_range(self) -> None:
        for window in (1, 3, 5, 7, 9, 11):
            result = self.service.filter_telemetry(
                {"median_window": window, "samples": [sample(0)]}
            )
            self.assertEqual(result["samples"][0]["filtered_current_a"], 1.0)

    def test_smoothing_factor_rules(self) -> None:
        for bad in (None, 0, -0.1, 1.01, True, "0.25", float("nan"), float("inf")):
            self.assert_code(
                {"smoothing_factor": bad, "samples": [sample(0)]},
                "invalid_filter_options",
            )

    def test_smoothing_factor_boundary_one_is_allowed(self) -> None:
        result = self.service.filter_telemetry(
            {
                "smoothing_factor": 1.0,
                "median_window": 1,
                "samples": [sample(0, 1.0), sample(1, 4.0)],
            }
        )
        self.assertEqual(result["samples"][1]["filtered_current_a"], 4.0)

    def test_reset_gap_rules(self) -> None:
        for bad in (None, 0, -1.0, True, "30", float("nan"), float("inf")):
            self.assert_code(
                {"reset_gap_s": bad, "samples": [sample(0)]},
                "invalid_filter_options",
            )


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
        status, body = self.post(TELEMETRY_FILTER_ROUTE, json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["sample_count"], 3)
        self.assertEqual(body["segment_count"], 1)
        self.assertEqual(len(body["samples"]), 3)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_FILTER_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_FILTER_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(TELEMETRY_FILTER_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_samples_is_422(self) -> None:
        status, body = self.post(
            TELEMETRY_FILTER_ROUTE, json.dumps({"samples": []}).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_invalid_filter_options_is_422(self) -> None:
        status, body = self.post(
            TELEMETRY_FILTER_ROUTE,
            json.dumps({"samples": [sample(0)], "median_window": 2}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_filter_options")

    def test_unknown_post_path_is_404(self) -> None:
        status, body = self.post("/v1/telemetry/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_existing_routes_still_work(self) -> None:
        status, body = self.post(HEALTH_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
