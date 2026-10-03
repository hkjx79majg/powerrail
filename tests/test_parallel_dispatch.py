import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import PARALLEL_DISPATCH_ROUTE, Handler
from powerrail.service import ApiError, Service


def pack(pack_id, voltage, max_current=10.0, fault=False):
    return {
        "id": pack_id,
        "voltage_v": voltage,
        "max_discharge_current_a": max_current,
        "fault": fault,
    }


def sample(timestamp, request, bus_voltage, packs):
    return {
        "timestamp_s": timestamp,
        "requested_bus_current_a": request,
        "bus_voltage_v": bus_voltage,
        "packs": packs,
    }


def config(**overrides):
    values = {"max_bus_voltage_delta_v": 0.5, "recovery_samples": 2}
    values.update(overrides)
    return values


def base_request(**overrides):
    payload = {
        "config": config(),
        "samples": [
            sample(0.0, 4.0, 48.0, [pack("a", 48.1), pack("b", 48.2)]),
            sample(1.0, 4.0, 48.0, [pack("a", 48.1), pack("b", 48.2)]),
        ],
    }
    payload.update(overrides)
    return payload


def by_id(decision):
    return {item["id"]: item for item in decision["pack_decisions"]}


class ParallelDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_equal_split_and_response_shape(self) -> None:
        result = self.service.dispatch_parallel_packs(base_request())
        self.assertEqual(len(result["decisions"]), 2)
        decision = result["decisions"][0]
        self.assertEqual(
            set(decision),
            {
                "timestamp_s",
                "pack_decisions",
                "allocated_bus_current_a",
                "unmet_bus_current_a",
                "status",
            },
        )
        self.assertEqual(decision["timestamp_s"], 0.0)
        self.assertEqual(decision["allocated_bus_current_a"], 4.0)
        self.assertEqual(decision["unmet_bus_current_a"], 0.0)
        self.assertEqual(decision["status"], "satisfied")
        packs = by_id(decision)
        self.assertEqual(packs["a"]["allocated_current_a"], 2.0)
        self.assertEqual(packs["b"]["allocated_current_a"], 2.0)
        self.assertEqual(packs["a"]["state"], "connected")
        self.assertEqual(packs["b"]["state"], "connected")

    def test_pack_decisions_follow_input_order(self) -> None:
        payload = base_request()
        payload["samples"][0]["packs"] = [pack("b", 48.2), pack("a", 48.1)]
        result = self.service.dispatch_parallel_packs(payload)
        ids = [item["id"] for item in result["decisions"][0]["pack_decisions"]]
        self.assertEqual(ids, ["b", "a"])

    def test_capped_pack_redistributes_remainder(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0.0,
                    10.0,
                    48.0,
                    [
                        pack("a", 48.0, max_current=3.0),
                        pack("b", 48.0, max_current=8.0),
                        pack("c", 48.0, max_current=8.0),
                    ],
                )
            ]
        )
        result = self.service.dispatch_parallel_packs(payload)
        decision = result["decisions"][0]
        packs = by_id(decision)
        self.assertEqual(packs["a"]["allocated_current_a"], 3.0)
        self.assertEqual(packs["b"]["allocated_current_a"], 3.5)
        self.assertEqual(packs["c"]["allocated_current_a"], 3.5)
        self.assertEqual(decision["allocated_bus_current_a"], 10.0)
        self.assertEqual(decision["status"], "satisfied")

    def test_all_capped_is_constrained(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0.0,
                    10.0,
                    48.0,
                    [pack("a", 48.0, max_current=2.0), pack("b", 48.0, max_current=3.0)],
                )
            ]
        )
        result = self.service.dispatch_parallel_packs(payload)
        decision = result["decisions"][0]
        self.assertEqual(decision["allocated_bus_current_a"], 5.0)
        self.assertEqual(decision["unmet_bus_current_a"], 5.0)
        self.assertEqual(decision["status"], "constrained")

    def test_fault_isolates_pack_immediately(self) -> None:
        payload = base_request(
            samples=[
                sample(0.0, 4.0, 48.0, [pack("a", 48.0, fault=True), pack("b", 48.0)]),
            ]
        )
        result = self.service.dispatch_parallel_packs(payload)
        packs = by_id(result["decisions"][0])
        self.assertEqual(packs["a"]["state"], "isolated")
        self.assertEqual(packs["a"]["allocated_current_a"], 0.0)
        self.assertEqual(packs["b"]["state"], "connected")
        self.assertEqual(packs["b"]["allocated_current_a"], 4.0)

    def test_voltage_delta_isolates_pack(self) -> None:
        payload = base_request(
            samples=[
                sample(0.0, 4.0, 48.0, [pack("a", 48.6), pack("b", 48.0)]),
            ]
        )
        result = self.service.dispatch_parallel_packs(payload)
        packs = by_id(result["decisions"][0])
        self.assertEqual(packs["a"]["state"], "isolated")
        self.assertEqual(packs["b"]["allocated_current_a"], 4.0)

    def test_voltage_delta_boundary_stays_connected(self) -> None:
        payload = base_request(
            samples=[
                sample(0.0, 4.0, 48.0, [pack("a", 48.5), pack("b", 48.0)]),
            ]
        )
        result = self.service.dispatch_parallel_packs(payload)
        packs = by_id(result["decisions"][0])
        self.assertEqual(packs["a"]["state"], "connected")
        self.assertEqual(packs["a"]["allocated_current_a"], 2.0)

    def test_recovery_after_consecutive_safe_samples(self) -> None:
        samples = [
            sample(0.0, 4.0, 48.0, [pack("a", 48.0, fault=True), pack("b", 48.0)]),
            sample(1.0, 4.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
            sample(2.0, 4.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
            sample(3.0, 4.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
        ]
        result = self.service.dispatch_parallel_packs(
            base_request(samples=samples)
        )
        states = [
            by_id(decision)["a"]["state"] for decision in result["decisions"]
        ]
        # isolated at 0, safe streak 1 at 1, streak 2 reconnects at 2
        self.assertEqual(states, ["isolated", "isolated", "connected", "connected"])
        packs = by_id(result["decisions"][2])
        self.assertEqual(packs["a"]["allocated_current_a"], 2.0)

    def test_unsafe_sample_resets_recovery_streak(self) -> None:
        samples = [
            sample(0.0, 4.0, 48.0, [pack("a", 48.0, fault=True), pack("b", 48.0)]),
            sample(1.0, 4.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
            sample(2.0, 4.0, 48.0, [pack("a", 49.0), pack("b", 48.0)]),
            sample(3.0, 4.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
            sample(4.0, 4.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
        ]
        result = self.service.dispatch_parallel_packs(
            base_request(samples=samples)
        )
        states = [
            by_id(decision)["a"]["state"] for decision in result["decisions"]
        ]
        # streak reset at sample 2 (delta 1.0 > 0.5), reconnects at sample 4
        self.assertEqual(
            states, ["isolated", "isolated", "isolated", "isolated", "connected"]
        )

    def test_recovery_samples_one_reconnects_next_safe_sample(self) -> None:
        samples = [
            sample(0.0, 4.0, 48.0, [pack("a", 48.0, fault=True), pack("b", 48.0)]),
            sample(1.0, 4.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
        ]
        result = self.service.dispatch_parallel_packs(
            base_request(samples=samples, config=config(recovery_samples=1))
        )
        states = [
            by_id(decision)["a"]["state"] for decision in result["decisions"]
        ]
        self.assertEqual(states, ["isolated", "connected"])

    def test_no_available_pack(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0.0,
                    4.0,
                    48.0,
                    [pack("a", 48.0, fault=True), pack("b", 48.0, fault=True)],
                )
            ]
        )
        result = self.service.dispatch_parallel_packs(payload)
        decision = result["decisions"][0]
        self.assertEqual(decision["allocated_bus_current_a"], 0.0)
        self.assertEqual(decision["unmet_bus_current_a"], 4.0)
        self.assertEqual(decision["status"], "no_available_pack")

    def test_zero_request_is_satisfied_even_without_packs_connected(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0.0,
                    0.0,
                    48.0,
                    [pack("a", 48.0, fault=True), pack("b", 48.0, fault=True)],
                )
            ]
        )
        result = self.service.dispatch_parallel_packs(payload)
        decision = result["decisions"][0]
        self.assertEqual(decision["allocated_bus_current_a"], 0.0)
        self.assertEqual(decision["unmet_bus_current_a"], 0.0)
        self.assertEqual(decision["status"], "satisfied")

    def test_isolated_pack_gets_zero_and_others_share(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0.0,
                    6.0,
                    48.0,
                    [
                        pack("a", 48.0, fault=True),
                        pack("b", 48.0),
                        pack("c", 48.0),
                    ],
                )
            ]
        )
        result = self.service.dispatch_parallel_packs(payload)
        packs = by_id(result["decisions"][0])
        self.assertEqual(packs["a"]["allocated_current_a"], 0.0)
        self.assertEqual(packs["b"]["allocated_current_a"], 3.0)
        self.assertEqual(packs["c"]["allocated_current_a"], 3.0)

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.dispatch_parallel_packs(payload)
        self.assertEqual(payload, snapshot)


class ParallelDispatchValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.dispatch_parallel_packs(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_config_rules(self) -> None:
        samples = base_request()["samples"]
        self.assert_code({"samples": samples}, "invalid_parallel_config")
        self.assert_code(
            {"config": None, "samples": samples}, "invalid_parallel_config"
        )
        self.assert_code(
            {"config": "x", "samples": samples}, "invalid_parallel_config"
        )
        for bad_delta in (None, -0.1, float("nan"), float("inf"), True, "0.5"):
            self.assert_code(
                {
                    "config": config(max_bus_voltage_delta_v=bad_delta),
                    "samples": samples,
                },
                "invalid_parallel_config",
            )
        for bad_recovery in (None, 0, -1, 1.5, True, "2"):
            self.assert_code(
                {"config": config(recovery_samples=bad_recovery), "samples": samples},
                "invalid_parallel_config",
            )

    def test_zero_voltage_delta_is_allowed(self) -> None:
        samples = [
            sample(0.0, 4.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
        ]
        result = self.service.dispatch_parallel_packs(
            base_request(samples=samples, config=config(max_bus_voltage_delta_v=0.0))
        )
        self.assertEqual(result["decisions"][0]["status"], "satisfied")

    def test_samples_structure_rules(self) -> None:
        self.assert_code({"config": config()}, "invalid_samples")
        self.assert_code({"config": config(), "samples": []}, "invalid_samples")
        self.assert_code({"config": config(), "samples": None}, "invalid_samples")
        self.assert_code({"config": config(), "samples": "x"}, "invalid_samples")
        self.assert_code({"config": config(), "samples": [1]}, "invalid_samples")

    def test_sample_field_rules(self) -> None:
        good = sample(0.0, 4.0, 48.0, [pack("a", 48.0)])
        for bad_timestamp in (None, float("nan"), float("inf"), True, "0"):
            bad = dict(good, timestamp_s=bad_timestamp)
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_samples"
            )
        for bad_request in (None, -1.0, float("nan"), float("inf"), True, "4"):
            bad = dict(good, requested_bus_current_a=bad_request)
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_samples"
            )
        for bad_bus in (None, 0.0, -48.0, float("nan"), float("inf"), True, "48"):
            bad = dict(good, bus_voltage_v=bad_bus)
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_samples"
            )

    def test_timestamps_must_be_strictly_increasing(self) -> None:
        packs = [pack("a", 48.0)]
        for second in (0.0, -1.0):
            samples = [
                sample(0.0, 1.0, 48.0, packs),
                sample(second, 1.0, 48.0, packs),
            ]
            self.assert_code(
                {"config": config(), "samples": samples}, "invalid_samples"
            )

    def test_packs_structure_rules(self) -> None:
        for bad_packs in (None, [], "x"):
            bad = sample(0.0, 1.0, 48.0, bad_packs)
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_packs"
            )
        bad = sample(0.0, 1.0, 48.0, [1])
        self.assert_code({"config": config(), "samples": [bad]}, "invalid_packs")

    def test_pack_id_rules(self) -> None:
        for bad_id in (None, "", 3):
            bad = sample(0.0, 1.0, 48.0, [pack(bad_id, 48.0)])
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_packs"
            )
        bad = sample(0.0, 1.0, 48.0, [pack("a", 48.0), pack("a", 48.1)])
        self.assert_code({"config": config(), "samples": [bad]}, "invalid_packs")

    def test_pack_id_set_must_match_across_samples(self) -> None:
        samples = [
            sample(0.0, 1.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
            sample(1.0, 1.0, 48.0, [pack("a", 48.0), pack("c", 48.0)]),
        ]
        self.assert_code({"config": config(), "samples": samples}, "invalid_packs")
        samples = [
            sample(0.0, 1.0, 48.0, [pack("a", 48.0)]),
            sample(1.0, 1.0, 48.0, [pack("a", 48.0), pack("b", 48.0)]),
        ]
        self.assert_code({"config": config(), "samples": samples}, "invalid_packs")

    def test_pack_field_rules(self) -> None:
        for bad_voltage in (None, 0.0, -1.0, float("nan"), float("inf"), True, "48"):
            bad = sample(0.0, 1.0, 48.0, [pack("a", bad_voltage)])
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_packs"
            )
        for bad_max in (None, -1.0, float("nan"), float("inf"), True, "10"):
            bad_pack = pack("a", 48.0, max_current=bad_max)
            bad = sample(0.0, 1.0, 48.0, [bad_pack])
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_packs"
            )
        for bad_fault in (None, 0, 1, "true"):
            bad_pack = pack("a", 48.0, fault=bad_fault)
            bad = sample(0.0, 1.0, 48.0, [bad_pack])
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_packs"
            )


class ParallelDispatchHttpTest(unittest.TestCase):
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
            PARALLEL_DISPATCH_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["decisions"]), 2)
        self.assertEqual(body["decisions"][0]["status"], "satisfied")

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(PARALLEL_DISPATCH_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(PARALLEL_DISPATCH_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(PARALLEL_DISPATCH_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_config_is_422(self) -> None:
        payload = {"config": {}, "samples": base_request()["samples"]}
        status, body = self.post(
            PARALLEL_DISPATCH_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_parallel_config")

    def test_invalid_samples_is_422(self) -> None:
        status, body = self.post(
            PARALLEL_DISPATCH_ROUTE,
            json.dumps({"config": config(), "samples": []}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_invalid_packs_is_422(self) -> None:
        bad = sample(0.0, 1.0, 48.0, [])
        status, body = self.post(
            PARALLEL_DISPATCH_ROUTE,
            json.dumps({"config": config(), "samples": [bad]}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_packs")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/packs/parallel/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
