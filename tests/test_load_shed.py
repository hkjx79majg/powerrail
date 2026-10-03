import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import LOAD_SHED_ROUTE, Handler
from powerrail.service import ApiError, Service


def load(id_, demand, minimum, priority, shed_level="never"):
    return {
        "id": id_,
        "demand_power_w": demand,
        "min_power_w": minimum,
        "priority": priority,
        "shed_level": shed_level,
    }


def sample(timestamp, available):
    return {"timestamp_s": timestamp, "available_power_w": available}


def base_request(**overrides):
    payload = {
        "loads": [
            load("a", 60.0, 10.0, 10, "never"),
            load("b", 50.0, 20.0, 5, "warning"),
            load("c", 30.0, 5.0, 8, "critical"),
        ],
        "config": {
            "reserve_power_w": 10.0,
            "warning_shortfall_w": 20.0,
            "critical_shortfall_w": 50.0,
            "recovery_samples": 2,
        },
        "samples": [sample(0.0, 200.0)],
    }
    payload.update(overrides)
    return payload


class LoadShedDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_no_shortfall_stays_normal_and_satisfied(self) -> None:
        result = self.service.decide_load_shedding(base_request())
        self.assertEqual(result["final_level"], "normal")
        self.assertEqual(result["escalation_count"], 0)
        decision = result["decisions"][0]
        self.assertEqual(decision["level"], "normal")
        self.assertEqual(decision["target_level"], "normal")
        self.assertAlmostEqual(decision["raw_shortfall_w"], 0.0)
        self.assertEqual(decision["shed_ids"], [])
        self.assertEqual(decision["status"], "satisfied")
        states = {a["id"]: a["state"] for a in decision["allocations"]}
        self.assertEqual(states, {"a": "powered", "b": "powered", "c": "powered"})

    def test_decisions_follow_sample_order(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(1.5, 200.0), sample(3.0, 200.0)])
        )
        self.assertEqual(
            [d["timestamp_s"] for d in result["decisions"]], [1.5, 3.0]
        )

    def test_warning_level_sheds_warning_loads_only(self) -> None:
        # raw shortfall = 140 + 10 - 120 = 30 -> warning.
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 120.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["level"], "warning")
        self.assertAlmostEqual(decision["raw_shortfall_w"], 30.0)
        self.assertEqual(decision["shed_ids"], ["b"])
        by_id = {a["id"]: a for a in decision["allocations"]}
        self.assertEqual(by_id["b"]["state"], "shed")
        self.assertAlmostEqual(by_id["b"]["allocated_power_w"], 0.0)
        self.assertAlmostEqual(by_id["b"]["shortfall_power_w"], 50.0)
        self.assertEqual(by_id["a"]["state"], "powered")
        self.assertEqual(by_id["c"]["state"], "powered")
        self.assertEqual(decision["status"], "satisfied")
        self.assertEqual(result["escalation_count"], 1)

    def test_critical_level_sheds_warning_and_critical_loads(self) -> None:
        # raw shortfall = 140 + 10 - 90 = 60 -> critical.
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 90.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["level"], "critical")
        self.assertEqual(decision["shed_ids"], ["b", "c"])
        by_id = {a["id"]: a for a in decision["allocations"]}
        self.assertEqual(by_id["a"]["state"], "powered")
        self.assertAlmostEqual(by_id["a"]["allocated_power_w"], 60.0)
        self.assertEqual(by_id["b"]["state"], "shed")
        self.assertEqual(by_id["c"]["state"], "shed")
        self.assertEqual(decision["status"], "satisfied")

    def test_two_level_jump_counts_as_one_escalation(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 90.0)])
        )
        self.assertEqual(result["final_level"], "critical")
        self.assertEqual(result["escalation_count"], 1)

    def test_escalation_is_immediate(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 200.0), sample(1.0, 120.0)])
        )
        self.assertEqual(
            [d["level"] for d in result["decisions"]], ["normal", "warning"]
        )
        self.assertEqual(result["escalation_count"], 1)

    def test_recovery_requires_consecutive_lower_samples(self) -> None:
        # recovery_samples = 2: warning at t=0, then lower targets.
        result = self.service.decide_load_shedding(
            base_request(
                samples=[
                    sample(0.0, 120.0),  # escalate to warning
                    sample(1.0, 200.0),  # lower, streak 1
                    sample(2.0, 120.0),  # target == level, streak reset
                    sample(3.0, 200.0),  # lower, streak 1
                    sample(4.0, 200.0),  # lower, streak 2 -> normal
                ]
            )
        )
        self.assertEqual(
            [d["level"] for d in result["decisions"]],
            ["warning", "warning", "warning", "warning", "normal"],
        )
        self.assertEqual(result["final_level"], "normal")
        self.assertEqual(result["escalation_count"], 1)

    def test_deescalation_drops_one_level_per_streak(self) -> None:
        config = {
            "reserve_power_w": 10.0,
            "warning_shortfall_w": 20.0,
            "critical_shortfall_w": 50.0,
            "recovery_samples": 1,
        }
        result = self.service.decide_load_shedding(
            base_request(
                config=config,
                samples=[
                    sample(0.0, 90.0),  # critical
                    sample(1.0, 200.0),  # drop to warning
                    sample(2.0, 200.0),  # drop to normal
                ],
            )
        )
        self.assertEqual(
            [d["level"] for d in result["decisions"]],
            ["critical", "warning", "normal"],
        )

    def test_reescalation_after_recovery_counts_again(self) -> None:
        config = {
            "reserve_power_w": 10.0,
            "warning_shortfall_w": 20.0,
            "critical_shortfall_w": 50.0,
            "recovery_samples": 1,
        }
        result = self.service.decide_load_shedding(
            base_request(
                config=config,
                samples=[
                    sample(0.0, 120.0),  # warning
                    sample(1.0, 200.0),  # normal
                    sample(2.0, 120.0),  # warning again
                ],
            )
        )
        self.assertEqual(result["escalation_count"], 2)
        self.assertEqual(result["final_level"], "warning")

    def test_constrained_when_budget_cannot_cover_remaining_loads(self) -> None:
        # raw shortfall = 140 + 10 - 135 = 15 -> normal; distributable 125.
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 135.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["level"], "normal")
        self.assertEqual(decision["status"], "constrained")
        by_id = {a["id"]: a for a in decision["allocations"]}
        # mins 10 + 20 + 5 = 35 funded; 90 left: a +50, c +25, b gets 15.
        self.assertAlmostEqual(by_id["a"]["allocated_power_w"], 60.0)
        self.assertAlmostEqual(by_id["c"]["allocated_power_w"], 30.0)
        self.assertAlmostEqual(by_id["b"]["allocated_power_w"], 35.0)
        self.assertEqual(by_id["b"]["state"], "limited")
        self.assertAlmostEqual(decision["allocated_power_w"], 125.0)
        self.assertAlmostEqual(decision["unallocated_power_w"], 0.0)

    def test_policy_shed_when_all_loads_shed(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(
                loads=[
                    load("b", 50.0, 20.0, 5, "warning"),
                    load("c", 30.0, 5.0, 8, "critical"),
                ],
                samples=[sample(0.0, 0.0)],
            )
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["level"], "critical")
        self.assertEqual(decision["status"], "policy_shed")
        for item in decision["allocations"]:
            self.assertEqual(item["state"], "shed")
            self.assertAlmostEqual(item["allocated_power_w"], 0.0)
        self.assertAlmostEqual(decision["allocated_power_w"], 0.0)

    def test_never_load_is_never_shed(self) -> None:
        # raw shortfall = 140 + 10 - 30 = 120 -> critical; distributable 20.
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 30.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["level"], "critical")
        self.assertEqual(decision["shed_ids"], ["b", "c"])
        by_id = {a["id"]: a for a in decision["allocations"]}
        self.assertEqual(by_id["a"]["state"], "limited")
        self.assertAlmostEqual(by_id["a"]["allocated_power_w"], 20.0)
        self.assertEqual(decision["status"], "constrained")

    def test_unallocated_leftover_when_demand_below_budget(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 500.0)])
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["status"], "satisfied")
        self.assertAlmostEqual(decision["allocated_power_w"], 140.0)
        self.assertAlmostEqual(decision["unallocated_power_w"], 350.0)

    def test_shortfall_floors_at_zero(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 500.0)])
        )
        self.assertAlmostEqual(result["decisions"][0]["raw_shortfall_w"], 0.0)

    def test_threshold_boundary_values(self) -> None:
        # raw shortfall exactly at warning/critical thresholds.
        result = self.service.decide_load_shedding(
            base_request(
                samples=[
                    sample(0.0, 130.0),  # raw 20 -> warning
                    sample(1.0, 100.0),  # raw 50 -> critical
                ]
            )
        )
        self.assertEqual(
            [d["target_level"] for d in result["decisions"]],
            ["warning", "critical"],
        )
        self.assertEqual(
            [d["level"] for d in result["decisions"]], ["warning", "critical"]
        )

    def test_allocations_follow_input_order(self) -> None:
        result = self.service.decide_load_shedding(
            base_request(samples=[sample(0.0, 120.0)])
        )
        self.assertEqual(
            [a["id"] for a in result["decisions"][0]["allocations"]],
            ["a", "b", "c"],
        )

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request(
            samples=[sample(0.0, 120.0), sample(1.0, 90.0), sample(2.0, 500.0)]
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
        missing = {k: v for k, v in base_request().items() if k != "loads"}
        self.assert_code(missing, "invalid_loads")

    def test_load_id_rules(self) -> None:
        self.assert_code(
            base_request(loads=[load("", 1.0, 0.0, 1)]), "invalid_load_id"
        )
        self.assert_code(
            base_request(loads=[load(None, 1.0, 0.0, 1)]), "invalid_load_id"
        )
        self.assert_code(
            base_request(loads=[load(7, 1.0, 0.0, 1)]), "invalid_load_id"
        )
        no_id = {"demand_power_w": 1.0, "min_power_w": 0.0, "priority": 1,
                 "shed_level": "never"}
        self.assert_code(base_request(loads=[no_id]), "invalid_load_id")
        self.assert_code(
            base_request(
                loads=[load("a", 1.0, 0.0, 1), load("a", 2.0, 0.0, 2)]
            ),
            "invalid_load_id",
        )

    def test_load_power_rules(self) -> None:
        def with_load(**fields):
            entry = load("a", 1.0, 0.0, 1)
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
        for key in ("demand_power_w", "min_power_w"):
            entry = load("a", 1.0, 0.0, 1)
            del entry[key]
            self.assert_code(base_request(loads=[entry]), "invalid_load_power")

    def test_priority_rules(self) -> None:
        def with_priority(priority):
            return base_request(loads=[load("a", 1.0, 0.0, priority)])

        for bad in (None, True, False, 1.5, "3", -1, 101, float("nan")):
            self.assert_code(with_priority(bad), "invalid_priority")
        entry = load("a", 1.0, 0.0, 1)
        del entry["priority"]
        self.assert_code(base_request(loads=[entry]), "invalid_priority")

    def test_shed_level_rules(self) -> None:
        def with_shed_level(shed_level):
            return base_request(loads=[load("a", 1.0, 0.0, 1, shed_level)])

        for bad in (None, True, 1, "", "normal", "WARNING", ["warning"]):
            self.assert_code(with_shed_level(bad), "invalid_shed_level")
        entry = load("a", 1.0, 0.0, 1)
        del entry["shed_level"]
        self.assert_code(base_request(loads=[entry]), "invalid_shed_level")
        for ok in ("warning", "critical", "never"):
            result = self.service.decide_load_shedding(with_shed_level(ok))
            self.assertEqual(result["final_level"], "normal")

    def test_config_rules(self) -> None:
        def with_config(**fields):
            config = {
                "reserve_power_w": 10.0,
                "warning_shortfall_w": 20.0,
                "critical_shortfall_w": 50.0,
                "recovery_samples": 2,
            }
            config.update(fields)
            return base_request(config=config)

        self.assert_code(base_request(config=None), "invalid_load_shed_config")
        self.assert_code(base_request(config="x"), "invalid_load_shed_config")
        missing = {k: v for k, v in base_request().items() if k != "config"}
        self.assert_code(missing, "invalid_load_shed_config")

        for bad in (None, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_config(reserve_power_w=bad), "invalid_load_shed_config"
            )
        for bad in (None, -1.0, 0.0, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_config(warning_shortfall_w=bad), "invalid_load_shed_config"
            )
            self.assert_code(
                with_config(critical_shortfall_w=bad), "invalid_load_shed_config"
            )
        # warning must be strictly below critical.
        self.assert_code(
            with_config(warning_shortfall_w=50.0), "invalid_load_shed_config"
        )
        self.assert_code(
            with_config(critical_shortfall_w=20.0), "invalid_load_shed_config"
        )
        for bad in (None, 0, -1, 1.5, True, "2", float("nan")):
            self.assert_code(
                with_config(recovery_samples=bad), "invalid_load_shed_config"
            )
        for key in (
            "reserve_power_w",
            "warning_shortfall_w",
            "critical_shortfall_w",
            "recovery_samples",
        ):
            config = {
                "reserve_power_w": 10.0,
                "warning_shortfall_w": 20.0,
                "critical_shortfall_w": 50.0,
                "recovery_samples": 2,
            }
            del config[key]
            self.assert_code(
                base_request(config=config), "invalid_load_shed_config"
            )

    def test_samples_rules(self) -> None:
        self.assert_code(base_request(samples=[]), "invalid_samples")
        self.assert_code(base_request(samples=None), "invalid_samples")
        self.assert_code(base_request(samples="x"), "invalid_samples")
        self.assert_code(base_request(samples=[1]), "invalid_samples")
        self.assert_code(base_request(samples=[None]), "invalid_samples")
        missing = {k: v for k, v in base_request().items() if k != "samples"}
        self.assert_code(missing, "invalid_samples")

    def test_timestamp_rules(self) -> None:
        for bad in (None, True, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(samples=[sample(bad, 100.0)]), "invalid_timestamp"
            )
        entry = {"available_power_w": 100.0}
        self.assert_code(
            base_request(samples=[entry]), "invalid_timestamp"
        )
        self.assert_code(
            base_request(samples=[sample(1.0, 100.0), sample(1.0, 100.0)]),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(samples=[sample(2.0, 100.0), sample(1.0, 100.0)]),
            "invalid_timestamp",
        )

    def test_available_power_rules(self) -> None:
        for bad in (None, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(samples=[sample(0.0, bad)]),
                "invalid_available_power",
            )
        entry = {"timestamp_s": 0.0}
        self.assert_code(
            base_request(samples=[entry]), "invalid_available_power"
        )

    def test_validation_order_loads_before_config_before_samples(self) -> None:
        self.assert_code(
            base_request(loads=[], config=None, samples=[]), "invalid_loads"
        )
        self.assert_code(
            base_request(config=None, samples=[]), "invalid_load_shed_config"
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

    def test_decide_route_returns_200(self) -> None:
        status, body = self.post(LOAD_SHED_ROUTE, json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["final_level"], "normal")
        self.assertEqual(body["escalation_count"], 0)
        self.assertEqual(len(body["decisions"]), 1)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(LOAD_SHED_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(LOAD_SHED_ROUTE, b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(LOAD_SHED_ROUTE, b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_shed_level_is_422(self) -> None:
        payload = base_request(loads=[load("a", 1.0, 0.0, 1, "normal")])
        status, body = self.post(LOAD_SHED_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_shed_level")
        self.assertTrue(body["error"]["message"])

    def test_invalid_config_is_422(self) -> None:
        payload = base_request(config={"reserve_power_w": -1.0})
        status, body = self.post(LOAD_SHED_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_load_shed_config")

    def test_invalid_available_power_is_422(self) -> None:
        payload = base_request(samples=[sample(0.0, True)])
        status, body = self.post(LOAD_SHED_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_available_power")

    def test_unknown_post_path_is_still_404(self) -> None:
        status, body = self.post("/v1/power/load-shed/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
