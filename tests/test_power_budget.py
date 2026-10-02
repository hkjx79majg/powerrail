import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import POWER_BUDGET_ROUTE, Handler
from powerrail.service import ApiError, Service


def load(id_, demand, minimum, priority):
    return {
        "id": id_,
        "demand_power_w": demand,
        "min_power_w": minimum,
        "priority": priority,
    }


def base_request(**overrides):
    payload = {
        "available_power_w": 110.0,
        "loads": [load("a", 60.0, 10.0, 10), load("b", 50.0, 20.0, 5)],
    }
    payload.update(overrides)
    return payload


class PowerBudgetAllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_all_demands_satisfied(self) -> None:
        result = self.service.allocate_power_budget(base_request())
        self.assertEqual(result["status"], "satisfied")
        self.assertAlmostEqual(result["allocated_power_w"], 110.0)
        self.assertAlmostEqual(result["unallocated_power_w"], 0.0)
        states = {a["id"]: a["state"] for a in result["allocations"]}
        self.assertEqual(states, {"a": "powered", "b": "powered"})
        for item in result["allocations"]:
            self.assertAlmostEqual(item["shortfall_power_w"], 0.0)

    def test_allocations_follow_input_order(self) -> None:
        result = self.service.allocate_power_budget(base_request())
        self.assertEqual([a["id"] for a in result["allocations"]], ["a", "b"])

    def test_reserve_reduces_distributable_power(self) -> None:
        result = self.service.allocate_power_budget(
            base_request(available_power_w=100.0, reserve_power_w=40.0)
        )
        # 60 W distributable: mins 10 + 20 = 30, then 30 left for demand
        # top-up: a (priority 10) gets 50 - 10 = 40 -> capped by remaining.
        by_id = {a["id"]: a for a in result["allocations"]}
        self.assertAlmostEqual(by_id["a"]["allocated_power_w"], 40.0)
        self.assertAlmostEqual(by_id["b"]["allocated_power_w"], 20.0)
        self.assertEqual(by_id["a"]["state"], "limited")
        self.assertEqual(by_id["b"]["state"], "limited")
        self.assertEqual(result["status"], "constrained")
        self.assertAlmostEqual(result["allocated_power_w"], 60.0)
        self.assertAlmostEqual(result["unallocated_power_w"], 0.0)

    def test_minimums_funded_before_lower_priority_demand(self) -> None:
        # 35 W: high min 10 funded, low min 20 funded, 5 left for high demand.
        result = self.service.allocate_power_budget(
            base_request(available_power_w=35.0)
        )
        by_id = {a["id"]: a for a in result["allocations"]}
        self.assertAlmostEqual(by_id["a"]["allocated_power_w"], 15.0)
        self.assertAlmostEqual(by_id["b"]["allocated_power_w"], 20.0)
        self.assertEqual(by_id["a"]["state"], "limited")
        self.assertEqual(by_id["b"]["state"], "limited")

    def test_same_level_minimums_split_proportionally(self) -> None:
        # Level mins total 30 but only 15 available -> 1:2 split, lower shed.
        result = self.service.allocate_power_budget(
            base_request(
                available_power_w=15.0,
                loads=[
                    load("a", 50.0, 10.0, 10),
                    load("b", 50.0, 20.0, 10),
                    load("c", 10.0, 5.0, 1),
                ],
            )
        )
        by_id = {a["id"]: a for a in result["allocations"]}
        self.assertAlmostEqual(by_id["a"]["allocated_power_w"], 5.0)
        self.assertAlmostEqual(by_id["b"]["allocated_power_w"], 10.0)
        self.assertAlmostEqual(by_id["c"]["allocated_power_w"], 0.0)
        self.assertEqual(by_id["a"]["state"], "limited")
        self.assertEqual(by_id["b"]["state"], "limited")
        self.assertEqual(by_id["c"]["state"], "shed")
        self.assertAlmostEqual(result["allocated_power_w"], 15.0)

    def test_same_level_demand_topup_split_by_unmet_need(self) -> None:
        # mins 10 + 10 = 20 funded; 20 left for needs 40 (a) and 20 (b) -> 2:1.
        result = self.service.allocate_power_budget(
            base_request(
                available_power_w=40.0,
                loads=[
                    load("a", 50.0, 10.0, 10),
                    load("b", 30.0, 10.0, 10),
                ],
            )
        )
        by_id = {a["id"]: a for a in result["allocations"]}
        self.assertAlmostEqual(by_id["a"]["allocated_power_w"], 10.0 + 40.0 / 3.0)
        self.assertAlmostEqual(by_id["b"]["allocated_power_w"], 10.0 + 20.0 / 3.0)
        self.assertAlmostEqual(result["allocated_power_w"], 40.0)

    def test_zero_demand_is_powered(self) -> None:
        result = self.service.allocate_power_budget(
            base_request(
                available_power_w=5.0,
                loads=[load("z", 0.0, 0.0, 0), load("a", 50.0, 10.0, 10)],
            )
        )
        by_id = {a["id"]: a for a in result["allocations"]}
        self.assertEqual(by_id["z"]["state"], "powered")
        self.assertAlmostEqual(by_id["z"]["allocated_power_w"], 0.0)
        self.assertEqual(by_id["a"]["state"], "limited")
        self.assertEqual(result["status"], "constrained")

    def test_shed_when_no_power(self) -> None:
        result = self.service.allocate_power_budget(
            base_request(available_power_w=0.0)
        )
        for item in result["allocations"]:
            self.assertEqual(item["state"], "shed")
            self.assertAlmostEqual(item["allocated_power_w"], 0.0)
        self.assertAlmostEqual(result["allocated_power_w"], 0.0)
        self.assertAlmostEqual(result["unallocated_power_w"], 0.0)
        self.assertEqual(result["status"], "constrained")

    def test_unallocated_leftover_when_demand_below_budget(self) -> None:
        result = self.service.allocate_power_budget(
            base_request(available_power_w=500.0, reserve_power_w=100.0)
        )
        self.assertEqual(result["status"], "satisfied")
        self.assertAlmostEqual(result["allocated_power_w"], 110.0)
        self.assertAlmostEqual(result["unallocated_power_w"], 290.0)

    def test_shuffling_loads_keeps_per_id_results(self) -> None:
        loads = [
            load("a", 50.0, 10.0, 10),
            load("b", 30.0, 10.0, 10),
            load("c", 20.0, 5.0, 20),
            load("d", 10.0, 0.0, 1),
        ]
        forward = self.service.allocate_power_budget(
            base_request(available_power_w=42.0, loads=loads)
        )
        backward = self.service.allocate_power_budget(
            base_request(available_power_w=42.0, loads=list(reversed(loads)))
        )
        forward_by_id = {a["id"]: a for a in forward["allocations"]}
        backward_by_id = {a["id"]: a for a in backward["allocations"]}
        self.assertEqual(forward_by_id, backward_by_id)
        self.assertEqual(
            [a["id"] for a in backward["allocations"]], ["d", "c", "b", "a"]
        )

    def test_call_does_not_modify_input(self) -> None:
        payload = base_request(available_power_w=42.0, reserve_power_w=2.0)
        snapshot = json.loads(json.dumps(payload))
        self.service.allocate_power_budget(payload)
        self.assertEqual(payload, snapshot)

    def test_allocations_never_exceed_demand_or_budget(self) -> None:
        result = self.service.allocate_power_budget(
            base_request(
                available_power_w=33.3,
                reserve_power_w=3.3,
                loads=[
                    load("a", 10.0, 3.0, 7),
                    load("b", 10.0, 3.0, 7),
                    load("c", 10.0, 3.0, 7),
                ],
            )
        )
        total = 0.0
        for item in result["allocations"]:
            self.assertGreaterEqual(item["allocated_power_w"], 0.0)
            self.assertLessEqual(item["allocated_power_w"], 10.0)
            total += item["allocated_power_w"]
        self.assertLessEqual(total, 30.0 + 1e-9)
        self.assertAlmostEqual(total, result["allocated_power_w"])


class PowerBudgetValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.allocate_power_budget(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5, 1.5, True):
            self.assert_code(payload, "invalid_json", status=400)

    def test_available_power_rules(self) -> None:
        self.assert_code(base_request(available_power_w=None), "invalid_budget")
        self.assert_code(base_request(available_power_w=-1.0), "invalid_budget")
        self.assert_code(base_request(available_power_w=True), "invalid_budget")
        self.assert_code(base_request(available_power_w="x"), "invalid_budget")
        self.assert_code(
            base_request(available_power_w=float("nan")), "invalid_budget"
        )
        self.assert_code(
            base_request(available_power_w=float("inf")), "invalid_budget"
        )
        missing = {k: v for k, v in base_request().items() if k != "available_power_w"}
        self.assert_code(missing, "invalid_budget")

    def test_reserve_power_rules(self) -> None:
        self.assert_code(base_request(reserve_power_w=-0.5), "invalid_budget")
        self.assert_code(base_request(reserve_power_w=False), "invalid_budget")
        self.assert_code(base_request(reserve_power_w="x"), "invalid_budget")
        self.assert_code(
            base_request(reserve_power_w=float("nan")), "invalid_budget"
        )
        self.assert_code(
            base_request(available_power_w=10.0, reserve_power_w=10.5),
            "invalid_budget",
        )
        # Reserve equal to available is allowed.
        result = self.service.allocate_power_budget(
            base_request(available_power_w=10.0, reserve_power_w=10.0)
        )
        self.assertAlmostEqual(result["allocated_power_w"], 0.0)

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
        no_id = {"demand_power_w": 1.0, "min_power_w": 0.0, "priority": 1}
        self.assert_code(base_request(loads=[no_id]), "invalid_load_id")
        self.assert_code(
            base_request(loads=[load("a", 1.0, 0.0, 1), load("a", 2.0, 0.0, 2)]),
            "invalid_load_id",
        )

    def test_load_power_rules(self) -> None:
        def with_load(**fields):
            entry = load("a", 1.0, 0.0, 1)
            entry.update(fields)
            return base_request(loads=[entry])

        for bad in (None, -1.0, True, "x", float("nan"), float("inf")):
            self.assert_code(
                with_load(demand_power_w=bad), "invalid_load_power"
            )
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
        # min equal to demand is allowed.
        result = self.service.allocate_power_budget(
            base_request(loads=[load("a", 5.0, 5.0, 1)])
        )
        self.assertEqual(result["allocations"][0]["state"], "powered")

    def test_priority_rules(self) -> None:
        def with_priority(priority):
            return base_request(loads=[load("a", 1.0, 0.0, priority)])

        for bad in (None, True, False, 1.5, "3", -1, 101, float("nan")):
            self.assert_code(with_priority(bad), "invalid_priority")
        entry = load("a", 1.0, 0.0, 1)
        del entry["priority"]
        self.assert_code(base_request(loads=[entry]), "invalid_priority")
        # Boundary values are allowed.
        for ok in (0, 100):
            result = self.service.allocate_power_budget(with_priority(ok))
            self.assertEqual(result["status"], "satisfied")


class PowerBudgetHttpTest(unittest.TestCase):
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

    def test_allocate_route_returns_200(self) -> None:
        status, body = self.post(POWER_BUDGET_ROUTE, json.dumps(base_request()).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "satisfied")
        self.assertEqual(len(body["allocations"]), 2)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(POWER_BUDGET_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(POWER_BUDGET_ROUTE, b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_array_is_invalid_json(self) -> None:
        status, body = self.post(POWER_BUDGET_ROUTE, b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_budget_is_422(self) -> None:
        payload = base_request(available_power_w=-1.0)
        status, body = self.post(POWER_BUDGET_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_budget")
        self.assertTrue(body["error"]["message"])

    def test_invalid_priority_is_422(self) -> None:
        payload = base_request(loads=[load("a", 1.0, 0.0, 101)])
        status, body = self.post(POWER_BUDGET_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_priority")

    def test_unknown_post_path_is_still_404(self) -> None:
        status, body = self.post("/v1/power/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
