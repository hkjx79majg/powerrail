import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import LOAD_SHED_ROUTE, Handler
from powerrail.service import ApiError, Service


_MISSING = object()


def load(id_, demand, minimum, priority, shed_level):
    return {
        "id": id_,
        "demand_power_w": demand,
        "min_power_w": minimum,
        "priority": priority,
        "shed_level": shed_level,
    }


def sample(timestamp, available):
    return {"timestamp_s": timestamp, "available_power_w": available}


def config(**overrides):
    values = {
        "reserve_power_w": 0.0,
        "warning_shortfall_w": 50.0,
        "critical_shortfall_w": 100.0,
        "recovery_samples": 2,
    }
    values.update(overrides)
    return values


def base_loads():
    return [
        load("a", 100.0, 10.0, 10, "warning"),
        load("b", 50.0, 20.0, 5, "critical"),
        load("c", 50.0, 0.0, 1, "never"),
    ]


def base_request(**overrides):
    payload = {"loads": base_loads(), "config": config(), "samples": [sample(0.0, 200.0)]}
    payload.update(overrides)
    return payload


def by_id(decision):
    return {item["id"]: item for item in decision["allocations"]}


class LoadShedDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_no_shortfall_is_normal_and_satisfied(self) -> None:
        result = self.service.decide_load_shedding(base_request())
        self.assertEqual(result["final_level"], "normal")
        self.assertEqual(result["escalation_count"], 0)
        decision = result["decisions"][0]
        self.assertEqual(
            set(decision),
            {
                "timestamp_s",
                "raw_shortfall_w",
                "target_level",
                "level",
                "allocated_power_w",
                "unallocated_power_w",
                "remaining_shortfall_w",
                "status",
                "allocations",
            },
        )
        self.assertEqual(decision["raw_shortfall_w"], 0.0)
        self.assertEqual(decision["target_level"], "normal")
        self.assertEqual(decision["level"], "normal")
        self.assertEqual(decision["status"], "satisfied")
        self.assertEqual(decision["allocated_power_w"], 200.0)
        self.assertEqual(decision["unallocated_power_w"], 0.0)
        self.assertEqual(decision["remaining_shortfall_w"], 0.0)
        allocations = by_id(decision)
        self.assertEqual(allocations["a"]["state"], "powered")
        self.assertEqual(allocations["b"]["state"], "powered")
        self.assertEqual(allocations["c"]["state"], "powered")

    def test_shortfall_floored_at_zero(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 500.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["raw_shortfall_w"], 0.0)
        self.assertEqual(decision["status"], "satisfied")
        self.assertEqual(decision["allocated_power_w"], 200.0)
        self.assertEqual(decision["unallocated_power_w"], 300.0)

    def test_warning_sheds_warning_loads_only(self) -> None:
        # available 150 -> raw shortfall 50, at the warning threshold.
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 150.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["target_level"], "warning")
        self.assertEqual(decision["level"], "warning")
        allocations = by_id(decision)
        self.assertEqual(allocations["a"]["state"], "shed")
        self.assertEqual(allocations["a"]["allocated_power_w"], 0.0)
        self.assertEqual(allocations["a"]["shortfall_power_w"], 100.0)
        self.assertEqual(allocations["b"]["state"], "powered")
        self.assertEqual(allocations["b"]["allocated_power_w"], 50.0)
        self.assertEqual(allocations["c"]["state"], "powered")
        self.assertEqual(allocations["c"]["allocated_power_w"], 50.0)
        self.assertEqual(decision["allocated_power_w"], 100.0)
        self.assertEqual(decision["unallocated_power_w"], 50.0)
        self.assertEqual(decision["remaining_shortfall_w"], 100.0)
        self.assertEqual(decision["status"], "constrained")
        self.assertEqual(result["final_level"], "warning")
        self.assertEqual(result["escalation_count"], 1)

    def test_critical_additionally_sheds_critical_loads(self) -> None:
        # available 90 -> raw shortfall 110, past the critical threshold.
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 90.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["target_level"], "critical")
        self.assertEqual(decision["level"], "critical")
        allocations = by_id(decision)
        self.assertEqual(allocations["a"]["state"], "shed")
        self.assertEqual(allocations["b"]["state"], "shed")
        self.assertEqual(allocations["c"]["state"], "powered")
        self.assertEqual(allocations["c"]["allocated_power_w"], 50.0)
        self.assertEqual(decision["allocated_power_w"], 50.0)
        self.assertEqual(decision["unallocated_power_w"], 40.0)
        self.assertEqual(decision["remaining_shortfall_w"], 150.0)
        self.assertEqual(decision["status"], "policy_shed")
        self.assertEqual(result["escalation_count"], 1)

    def test_never_loads_are_never_shed(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(
                samples=[sample(0.0, 0.0)],
                config=config(reserve_power_w=0.0),
            )
        )
        decision = result["decisions"][0]
        allocations = by_id(decision)
        self.assertEqual(allocations["a"]["state"], "shed")
        self.assertEqual(allocations["b"]["state"], "shed")
        self.assertEqual(allocations["c"]["state"], "shed")
        self.assertEqual(allocations["c"]["allocated_power_w"], 0.0)
        self.assertEqual(decision["status"], "policy_shed")

    def test_two_level_escalation_counts_once(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 50.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["level"], "critical")
        self.assertEqual(result["escalation_count"], 1)
        self.assertEqual(result["final_level"], "critical")

    def test_threshold_boundaries(self) -> None:
        # raw shortfall 49 -> below warning threshold.
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 151.0)])
        )
        self.assertEqual(result["decisions"][0]["target_level"], "normal")
        # raw shortfall 100 -> at critical threshold.
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 100.0)])
        )
        self.assertEqual(result["decisions"][0]["target_level"], "critical")

    def test_reserve_counts_into_shortfall_and_budget(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(
                config=config(reserve_power_w=20.0),
                samples=[sample(0.0, 200.0)],
            )
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["raw_shortfall_w"], 20.0)
        self.assertEqual(decision["level"], "normal")
        # 180 W distributable: mins 30, then demand top-up leaves c 30 W short.
        allocations = by_id(decision)
        self.assertEqual(allocations["a"]["allocated_power_w"], 100.0)
        self.assertEqual(allocations["b"]["allocated_power_w"], 50.0)
        self.assertEqual(allocations["c"]["allocated_power_w"], 30.0)
        self.assertEqual(decision["status"], "constrained")

    def test_hysteresis_deescalation_sequence(self) -> None:
        samples = [
            sample(0.0, 90.0),   # shortfall 110 -> escalate to critical
            sample(1.0, 200.0),  # shortfall 0, recovery streak 1, stays critical
            sample(2.0, 150.0),  # shortfall 50 (< 100), streak 2 -> drop to warning
            sample(3.0, 200.0),  # shortfall 0 (< 50), streak 1, stays warning
            sample(4.0, 200.0),  # streak 2 -> drop to normal
        ]
        result = self.service.decide_load_shedding(base_request(samples=samples))
        levels = [d["level"] for d in result["decisions"]]
        targets = [d["target_level"] for d in result["decisions"]]
        self.assertEqual(
            levels, ["critical", "critical", "warning", "warning", "normal"]
        )
        self.assertEqual(
            targets, ["critical", "normal", "warning", "normal", "normal"]
        )
        self.assertEqual(result["final_level"], "normal")
        self.assertEqual(result["escalation_count"], 1)
        # While lingering in critical, warning/critical loads stay shed.
        lingering = by_id(result["decisions"][1])
        self.assertEqual(lingering["a"]["state"], "shed")
        self.assertEqual(lingering["b"]["state"], "shed")
        self.assertEqual(lingering["c"]["state"], "powered")
        self.assertEqual(result["decisions"][1]["status"], "policy_shed")
        # One level recovered: warning load still shed, critical load repowered.
        warning = by_id(result["decisions"][2])
        self.assertEqual(warning["a"]["state"], "shed")
        self.assertEqual(warning["b"]["state"], "powered")
        self.assertEqual(warning["c"]["state"], "powered")
        self.assertEqual(result["decisions"][2]["status"], "constrained")
        # Back to normal with everything powered.
        self.assertEqual(result["decisions"][4]["status"], "satisfied")

    def test_non_recovering_sample_resets_streak(self) -> None:
        samples = [
            sample(0.0, 90.0),   # to critical (shortfall 110)
            sample(1.0, 110.0),  # shortfall 90 < 100, streak 1
            sample(2.0, 100.0),  # shortfall 100 -> target critical, streak reset
            sample(3.0, 200.0),  # shortfall 0, streak 1
            sample(4.0, 90.0),   # shortfall 110 -> target critical, streak reset
            sample(5.0, 200.0),  # shortfall 0, streak 1 again
            sample(6.0, 200.0),  # streak 2 -> finally drop to warning
        ]
        result = self.service.decide_load_shedding(base_request(samples=samples))
        levels = [d["level"] for d in result["decisions"]]
        self.assertEqual(
            levels,
            ["critical", "critical", "critical", "critical", "critical",
             "critical", "warning"],
        )
        self.assertEqual(result["escalation_count"], 1)

    def test_reescalation_is_counted_again(self) -> None:
        samples = [
            sample(0.0, 150.0),  # to warning
            sample(1.0, 200.0),  # streak 1, stays warning
            sample(2.0, 200.0),  # streak 2 -> normal
            sample(3.0, 150.0),  # to warning again
        ]
        result = self.service.decide_load_shedding(base_request(samples=samples))
        self.assertEqual(
            [d["level"] for d in result["decisions"]],
            ["warning", "warning", "normal", "warning"],
        )
        self.assertEqual(result["escalation_count"], 2)
        self.assertEqual(result["final_level"], "warning")

    def test_recovery_samples_one_deescalates_next_sample(self) -> None:
        samples = [sample(0.0, 90.0), sample(1.0, 200.0)]
        result = self.service.decide_load_shedding(
            base_request(samples=samples, config=config(recovery_samples=1))
        )
        self.assertEqual(
            [d["level"] for d in result["decisions"]], ["critical", "warning"]
        )

    def test_decisions_follow_sample_order(self) -> None:
        samples = [sample(3.0, 200.0), sample(7.0, 200.0)]
        result = self.service.decide_load_shedding(base_request(samples=samples))
        self.assertEqual(
            [d["timestamp_s"] for d in result["decisions"]], [3.0, 7.0]
        )
        self.assertEqual(
            [a["id"] for a in result["decisions"][0]["allocations"]],
            ["a", "b", "c"],
        )

    def test_remaining_loads_follow_budget_rules(self) -> None:
        # warning: a (warning, 100 W) shed. 150 W distributable to b (min 20)
        # and c (min 0): b minimum first, then demand top-up by priority.
        result = self.service.decide_load_shedding(
            base_request(
                loads=[
                    load("a", 100.0, 10.0, 10, "warning"),
                    load("b", 50.0, 20.0, 5, "never"),
                    load("c", 50.0, 0.0, 1, "never"),
                ],
                samples=[sample(0.0, 60.0)],  # shortfall 140 -> critical
            )
        )
        decision = result["decisions"][0]
        allocations = by_id(decision)
        # a and b: a shed; b is never so it stays; c never.
        self.assertEqual(allocations["a"]["state"], "shed")
        # 60 W to b/c: b min 20, then b demand +30 (prio 5), c gets 10.
        self.assertAlmostEqual(allocations["b"]["allocated_power_w"], 50.0)
        self.assertAlmostEqual(allocations["c"]["allocated_power_w"], 10.0)
        self.assertEqual(allocations["c"]["state"], "limited")
        self.assertEqual(decision["status"], "policy_shed")

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request(
            samples=[sample(0.0, 90.0), sample(1.0, 200.0)],
            config=config(reserve_power_w=5.0),
        )
        snapshot = json.loads(json.dumps(payload))
        self.service.decide_load_shedding(payload)
        self.assertEqual(payload, snapshot)


class LoadShedValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.decide_load_shedding(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5, 1.5, True):
            self.assert_code(payload, "invalid_json", status=400)

    def test_loads_rules(self) -> None:
        self.assert_code(base_request(loads=[]), "invalid_loads")
        self.assert_code(base_request(loads=None), "invalid_loads")
        self.assert_code(base_request(loads="x"), "invalid_loads")
        self.assert_code(base_request(loads=[1, 2]), "invalid_loads")
        self.assert_code(base_request(loads=[None]), "invalid_loads")

    def test_load_id_rules(self) -> None:
        for bad_id in ("", None, 7):
            self.assert_code(
                base_request(loads=[load(bad_id, 1.0, 0.0, 1, "never")]),
                "invalid_load_id",
            )
        duplicate = [
            load("a", 1.0, 0.0, 1, "never"),
            load("a", 2.0, 0.0, 2, "never"),
        ]
        self.assert_code(base_request(loads=duplicate), "invalid_load_id")

    def test_load_power_rules(self) -> None:
        def with_load(**fields):
            entry = load("a", 1.0, 0.0, 1, "never")
            entry.update(fields)
            return base_request(loads=[entry])

        for bad in (None, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(with_load(demand_power_w=bad), "invalid_load_power")
            self.assert_code(
                with_load(demand_power_w=5.0, min_power_w=bad),
                "invalid_load_power",
            )
        self.assert_code(
            with_load(demand_power_w=5.0, min_power_w=5.5), "invalid_load_power"
        )

    def test_priority_rules(self) -> None:
        def with_priority(priority):
            return base_request(
                loads=[load("a", 1.0, 0.0, priority, "never")]
            )

        for bad in (None, True, False, 1.5, "3", -1, 101):
            self.assert_code(with_priority(bad), "invalid_priority")

    def test_shed_level_rules(self) -> None:
        def with_level(level):
            entry = load("a", 1.0, 0.0, 1, "never")
            if level is not _MISSING:
                entry["shed_level"] = level
            else:
                del entry["shed_level"]
            return base_request(loads=[entry])

        for bad in ("normal", "warn", "", None, True, 1, _MISSING):
            self.assert_code(with_level(bad), "invalid_shed_level")
        # The documented values are accepted.
        for ok in ("warning", "critical", "never"):
            result = self.service.decide_load_shedding(with_level(ok))
            self.assertEqual(result["final_level"], "normal")

    def test_config_rules(self) -> None:
        samples = base_request()["samples"]
        loads = base_request()["loads"]
        self.assert_code({"loads": loads, "samples": samples}, "invalid_load_shed_config")
        self.assert_code(
            {"loads": loads, "config": None, "samples": samples},
            "invalid_load_shed_config",
        )
        self.assert_code(
            {"loads": loads, "config": "x", "samples": samples},
            "invalid_load_shed_config",
        )
        for bad_reserve in (None, -1.0, True, "0", float("nan"), float("inf")):
            self.assert_code(
                {"loads": loads, "config": config(reserve_power_w=bad_reserve),
                 "samples": samples},
                "invalid_load_shed_config",
            )
        bad_thresholds = [
            config(warning_shortfall_w=0.0),
            config(warning_shortfall_w=-1.0),
            config(warning_shortfall_w=100.0, critical_shortfall_w=100.0),
            config(warning_shortfall_w=120.0, critical_shortfall_w=100.0),
            config(warning_shortfall_w=True, critical_shortfall_w=100.0),
            config(warning_shortfall_w=50.0, critical_shortfall_w=float("nan")),
        ]
        for bad_config in bad_thresholds:
            self.assert_code(
                {"loads": loads, "config": bad_config, "samples": samples},
                "invalid_load_shed_config",
            )
        for bad_recovery in (None, 0, -1, 1.5, True, "2"):
            self.assert_code(
                {"loads": loads, "config": config(recovery_samples=bad_recovery),
                 "samples": samples},
                "invalid_load_shed_config",
            )

    def test_samples_rules(self) -> None:
        self.assert_code(
            {"loads": base_loads(), "config": config()}, "invalid_samples"
        )
        self.assert_code(
            base_request(samples=[]), "invalid_samples"
        )
        self.assert_code(base_request(samples=None), "invalid_samples")
        self.assert_code(base_request(samples="x"), "invalid_samples")
        self.assert_code(base_request(samples=[1]), "invalid_samples")
        self.assert_code(base_request(samples=[None]), "invalid_samples")

    def test_timestamp_rules(self) -> None:
        for bad in (None, float("nan"), float("inf"), True, "0"):
            self.assert_code(
                base_request(samples=[{"timestamp_s": bad,
                                       "available_power_w": 1.0}]),
                "invalid_timestamp",
            )
        self.assert_code(
            base_request(
                samples=[sample(0.0, 200.0), sample(0.0, 200.0)]
            ),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(
                samples=[sample(1.0, 200.0), sample(0.0, 200.0)]
            ),
            "invalid_timestamp",
        )

    def test_available_power_rules(self) -> None:
        for bad in (None, -1.0, True, "100", float("nan"), float("inf")):
            self.assert_code(
                base_request(samples=[{"timestamp_s": 0.0,
                                       "available_power_w": bad}]),
                "invalid_available_power",
            )


class LoadShedHttpTest(unittest.TestCase):
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
            LOAD_SHED_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["decisions"]), 1)
        self.assertEqual(body["final_level"], "normal")
        self.assertEqual(body["decisions"][0]["status"], "satisfied")

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(LOAD_SHED_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(LOAD_SHED_ROUTE, b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(LOAD_SHED_ROUTE, b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_shed_level_is_422(self) -> None:
        payload = base_request(
            loads=[load("a", 1.0, 0.0, 1, "urgent")]
        )
        status, body = self.post(
            LOAD_SHED_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_shed_level")

    def test_invalid_load_shed_config_is_422(self) -> None:
        payload = base_request(config=config(recovery_samples=0))
        status, body = self.post(
            LOAD_SHED_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_load_shed_config")

    def test_invalid_available_power_is_422(self) -> None:
        payload = base_request(samples=[{"timestamp_s": 0.0,
                                         "available_power_w": -1.0}])
        status, body = self.post(
            LOAD_SHED_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_available_power")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/power/load-shed/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
