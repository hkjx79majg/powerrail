import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import PARALLEL_DISPATCH_ROUTE, Handler
from powerrail.service import ApiError, Service


def config(**overrides):
    cfg = {
        "max_bus_voltage_delta_v": 0.5,
        "recovery_samples": 2,
    }
    cfg.update(overrides)
    return cfg


def pack(pack_id, voltage=48.0, max_current=10.0, fault=False):
    return {
        "id": pack_id,
        "voltage_v": voltage,
        "max_discharge_current_a": max_current,
        "fault": fault,
    }


_DEFAULT_PACKS = object()


def sample(timestamp, requested=8.0, bus_voltage=48.0, packs=_DEFAULT_PACKS):
    if packs is _DEFAULT_PACKS:
        packs = [pack("a"), pack("b")]
    return {
        "timestamp_s": timestamp,
        "requested_bus_current_a": requested,
        "bus_voltage_v": bus_voltage,
        "packs": packs,
    }


def base_request(**overrides):
    payload = {
        "config": config(),
        "samples": [
            sample(0),
            sample(1),
            sample(2),
        ],
    }
    payload.update(overrides)
    return payload


def states(decision):
    return [item["state"] for item in decision["pack_decisions"]]


def currents(decision):
    return [item["allocated_current_a"] for item in decision["pack_decisions"]]


class ParallelDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_response_shape_and_equal_split(self) -> None:
        result = self.service.dispatch_parallel(base_request())
        decisions = result["decisions"]
        self.assertEqual(len(decisions), 3)
        self.assertEqual(
            [item["timestamp_s"] for item in decisions], [0, 1, 2]
        )
        for item in decisions:
            self.assertEqual(
                set(item),
                {
                    "timestamp_s",
                    "pack_decisions",
                    "allocated_bus_current_a",
                    "unmet_bus_current_a",
                    "status",
                },
            )
            self.assertEqual(states(item), ["connected", "connected"])
            self.assertEqual(currents(item), [4.0, 4.0])
            self.assertEqual(item["allocated_bus_current_a"], 8.0)
            self.assertEqual(item["unmet_bus_current_a"], 0.0)
            self.assertEqual(item["status"], "satisfied")
            for pack_decision in item["pack_decisions"]:
                self.assertEqual(
                    set(pack_decision),
                    {"id", "allocated_current_a", "state"},
                )

    def test_pack_decisions_follow_input_order(self) -> None:
        payload = base_request(
            samples=[sample(0, packs=[pack("b"), pack("a"), pack("c")])]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(
            [item["id"] for item in decision["pack_decisions"]],
            ["b", "a", "c"],
        )

    def test_capped_pack_redistributes_remainder(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0,
                    requested=10.0,
                    packs=[pack("a", max_current=3.0), pack("b")],
                )
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(currents(decision), [3.0, 7.0])
        self.assertEqual(decision["allocated_bus_current_a"], 10.0)
        self.assertEqual(decision["status"], "satisfied")

    def test_all_packs_capped_is_constrained(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0,
                    requested=10.0,
                    packs=[pack("a", max_current=3.0), pack("b", max_current=4.0)],
                )
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(currents(decision), [3.0, 4.0])
        self.assertEqual(decision["allocated_bus_current_a"], 7.0)
        self.assertEqual(decision["unmet_bus_current_a"], 3.0)
        self.assertEqual(decision["status"], "constrained")

    def test_fault_isolates_immediately(self) -> None:
        payload = base_request(
            samples=[
                sample(0, packs=[pack("a", fault=True), pack("b")]),
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(states(decision), ["isolated", "connected"])
        self.assertEqual(currents(decision), [0.0, 8.0])
        self.assertEqual(decision["status"], "satisfied")

    def test_voltage_delta_isolates_immediately(self) -> None:
        payload = base_request(
            samples=[
                sample(0, packs=[pack("a", voltage=49.0), pack("b")]),
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(states(decision), ["isolated", "connected"])
        self.assertEqual(currents(decision), [0.0, 8.0])

    def test_voltage_delta_at_threshold_stays_connected(self) -> None:
        payload = base_request(
            samples=[
                sample(0, packs=[pack("a", voltage=48.5), pack("b")]),
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(states(decision), ["connected", "connected"])
        self.assertEqual(currents(decision), [4.0, 4.0])

    def test_recovery_after_consecutive_safe_samples(self) -> None:
        payload = base_request(
            samples=[
                sample(0, packs=[pack("a", fault=True), pack("b")]),
                sample(1, packs=[pack("a"), pack("b")]),
                sample(2, packs=[pack("a"), pack("b")]),
                sample(3, packs=[pack("a"), pack("b")]),
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decisions = result["decisions"]
        self.assertEqual(states(decisions[0]), ["isolated", "connected"])
        self.assertEqual(states(decisions[1]), ["isolated", "connected"])
        # Second consecutive safe sample: reconnects at this sample.
        self.assertEqual(states(decisions[2]), ["connected", "connected"])
        self.assertEqual(currents(decisions[2]), [4.0, 4.0])
        self.assertEqual(states(decisions[3]), ["connected", "connected"])

    def test_unsafe_sample_resets_recovery_streak(self) -> None:
        payload = base_request(
            samples=[
                sample(0, packs=[pack("a", fault=True), pack("b")]),
                sample(1, packs=[pack("a"), pack("b")]),
                sample(2, packs=[pack("a", voltage=49.0), pack("b")]),
                sample(3, packs=[pack("a"), pack("b")]),
                sample(4, packs=[pack("a"), pack("b")]),
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decisions = result["decisions"]
        self.assertEqual(states(decisions[1]), ["isolated", "connected"])
        # Unsafe again: streak cleared.
        self.assertEqual(states(decisions[2]), ["isolated", "connected"])
        self.assertEqual(states(decisions[3]), ["isolated", "connected"])
        # Two consecutive safe samples after the reset: reconnects.
        self.assertEqual(states(decisions[4]), ["connected", "connected"])

    def test_recovery_samples_one_reconnects_on_first_safe_sample(self) -> None:
        payload = base_request(
            config=config(recovery_samples=1),
            samples=[
                sample(0, packs=[pack("a", fault=True), pack("b")]),
                sample(1, packs=[pack("a"), pack("b")]),
            ],
        )
        result = self.service.dispatch_parallel(payload)
        decisions = result["decisions"]
        self.assertEqual(states(decisions[0]), ["isolated", "connected"])
        self.assertEqual(states(decisions[1]), ["connected", "connected"])
        self.assertEqual(currents(decisions[1]), [4.0, 4.0])

    def test_no_available_pack(self) -> None:
        payload = base_request(
            samples=[
                sample(0, packs=[pack("a", fault=True), pack("b", fault=True)]),
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(states(decision), ["isolated", "isolated"])
        self.assertEqual(decision["allocated_bus_current_a"], 0.0)
        self.assertEqual(decision["unmet_bus_current_a"], 8.0)
        self.assertEqual(decision["status"], "no_available_pack")

    def test_zero_request_without_connected_packs_is_satisfied(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0,
                    requested=0.0,
                    packs=[pack("a", fault=True), pack("b", fault=True)],
                ),
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(decision["allocated_bus_current_a"], 0.0)
        self.assertEqual(decision["unmet_bus_current_a"], 0.0)
        self.assertEqual(decision["status"], "satisfied")

    def test_zero_request_is_satisfied_with_equal_zero_shares(self) -> None:
        result = self.service.dispatch_parallel(
            base_request(samples=[sample(0, requested=0.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(currents(decision), [0.0, 0.0])
        self.assertEqual(decision["status"], "satisfied")

    def test_isolated_pack_gets_zero_and_rest_share_request(self) -> None:
        payload = base_request(
            samples=[
                sample(
                    0,
                    requested=9.0,
                    packs=[
                        pack("a", fault=True),
                        pack("b", max_current=4.0),
                        pack("c"),
                    ],
                ),
            ]
        )
        result = self.service.dispatch_parallel(payload)
        decision = result["decisions"][0]
        self.assertEqual(states(decision), ["isolated", "connected", "connected"])
        self.assertEqual(currents(decision), [0.0, 4.0, 5.0])
        self.assertEqual(decision["status"], "satisfied")

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.dispatch_parallel(payload)
        self.assertEqual(payload, snapshot)

    def test_timestamp_is_preserved_verbatim(self) -> None:
        payload = base_request(samples=[sample(7)])
        result = self.service.dispatch_parallel(payload)
        self.assertEqual(result["decisions"][0]["timestamp_s"], 7)
        self.assertIsInstance(result["decisions"][0]["timestamp_s"], int)


class ParallelDispatchValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.dispatch_parallel(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_config_rules(self) -> None:
        good_samples = [sample(0)]
        self.assert_code({"samples": good_samples}, "invalid_parallel_config")
        self.assert_code(
            {"config": None, "samples": good_samples}, "invalid_parallel_config"
        )
        self.assert_code(
            {"config": "x", "samples": good_samples}, "invalid_parallel_config"
        )
        for bad_delta in (None, -0.1, float("nan"), float("inf"), True, "0.5"):
            self.assert_code(
                {
                    "config": config(max_bus_voltage_delta_v=bad_delta),
                    "samples": good_samples,
                },
                "invalid_parallel_config",
            )
        for bad_recovery in (None, 0, -1, 1.5, True, "2"):
            self.assert_code(
                {
                    "config": config(recovery_samples=bad_recovery),
                    "samples": good_samples,
                },
                "invalid_parallel_config",
            )

    def test_samples_rules(self) -> None:
        self.assert_code({"config": config()}, "invalid_samples")
        self.assert_code(
            {"config": config(), "samples": []}, "invalid_samples"
        )
        self.assert_code(
            {"config": config(), "samples": None}, "invalid_samples"
        )
        self.assert_code(
            {"config": config(), "samples": "x"}, "invalid_samples"
        )
        self.assert_code(
            {"config": config(), "samples": [1]}, "invalid_samples"
        )

    def test_timestamp_rules(self) -> None:
        for bad_timestamp in (None, float("nan"), float("inf"), True, "1"):
            self.assert_code(
                {"config": config(), "samples": [sample(bad_timestamp)]},
                "invalid_samples",
            )
        self.assert_code(
            {"config": config(), "samples": [sample(1), sample(1)]},
            "invalid_samples",
        )
        self.assert_code(
            {"config": config(), "samples": [sample(2), sample(1)]},
            "invalid_samples",
        )

    def test_requested_bus_current_rules(self) -> None:
        for bad_current in (None, -0.5, float("nan"), float("inf"), True, "5"):
            self.assert_code(
                {"config": config(), "samples": [sample(0, requested=bad_current)]},
                "invalid_samples",
            )

    def test_bus_voltage_rules(self) -> None:
        for bad_voltage in (None, 0.0, -1.0, float("nan"), float("inf"), False, "48"):
            self.assert_code(
                {
                    "config": config(),
                    "samples": [sample(0, bus_voltage=bad_voltage)],
                },
                "invalid_samples",
            )

    def test_packs_structure_rules(self) -> None:
        self.assert_code(
            {"config": config(), "samples": [sample(0, packs=[])]},
            "invalid_packs",
        )
        self.assert_code(
            {"config": config(), "samples": [sample(0, packs=None)]},
            "invalid_packs",
        )
        self.assert_code(
            {"config": config(), "samples": [sample(0, packs="x")]},
            "invalid_packs",
        )
        self.assert_code(
            {"config": config(), "samples": [sample(0, packs=[1])]},
            "invalid_packs",
        )

    def test_pack_id_rules(self) -> None:
        for bad_id in (None, "", 5, True):
            self.assert_code(
                {"config": config(), "samples": [sample(0, packs=[pack(bad_id)])]},
                "invalid_packs",
            )
        self.assert_code(
            {
                "config": config(),
                "samples": [sample(0, packs=[pack("a"), pack("a")])],
            },
            "invalid_packs",
        )

    def test_pack_id_set_must_match_across_samples(self) -> None:
        self.assert_code(
            {
                "config": config(),
                "samples": [
                    sample(0, packs=[pack("a"), pack("b")]),
                    sample(1, packs=[pack("a"), pack("c")]),
                ],
            },
            "invalid_packs",
        )
        self.assert_code(
            {
                "config": config(),
                "samples": [
                    sample(0, packs=[pack("a"), pack("b")]),
                    sample(1, packs=[pack("a")]),
                ],
            },
            "invalid_packs",
        )

    def test_pack_voltage_rules(self) -> None:
        for bad_voltage in (None, 0.0, -1.0, float("nan"), float("inf"), False, "48"):
            self.assert_code(
                {
                    "config": config(),
                    "samples": [sample(0, packs=[pack("a", voltage=bad_voltage)])],
                },
                "invalid_packs",
            )

    def test_pack_max_current_rules(self) -> None:
        for bad_current in (None, -0.5, float("nan"), float("inf"), True, "5"):
            self.assert_code(
                {
                    "config": config(),
                    "samples": [
                        sample(0, packs=[pack("a", max_current=bad_current)])
                    ],
                },
                "invalid_packs",
            )

    def test_pack_fault_rules(self) -> None:
        for bad_fault in (None, 0, 1, "false"):
            bad_pack = pack("a")
            bad_pack["fault"] = bad_fault
            self.assert_code(
                {"config": config(), "samples": [sample(0, packs=[bad_pack])]},
                "invalid_packs",
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
        self.assertEqual(len(body["decisions"]), 3)
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
        status, body = self.post(
            PARALLEL_DISPATCH_ROUTE,
            json.dumps({"config": {}, "samples": [sample(0)]}).encode(),
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
        status, body = self.post(
            PARALLEL_DISPATCH_ROUTE,
            json.dumps(
                {"config": config(), "samples": [sample(0, packs=[])]}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_packs")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/packs/parallel/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
