import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import POWER_BUDGET_ROUTE, Handler
from powerrail.service import ApiError, Service


def base_request(**overrides):
    payload = {
        "available_power_w": 100.0,
        "loads": [
            {"id": "a", "demand_power_w": 40.0, "min_power_w": 10.0, "priority": 10},
            {"id": "b", "demand_power_w": 30.0, "min_power_w": 5.0, "priority": 20},
        ],
    }
    payload.update(overrides)
    return payload


class AllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def by_id(self, result):
        return {item["id"]: item for item in result["allocations"]}

    def test_full_demand_is_satisfied(self) -> None:
        result = self.service.allocate_power_budget(base_request())
        self.assertEqual(result["status"], "satisfied")
        self.assertAlmostEqual(result["allocated_power_w"], 70.0)
        self.assertAlmostEqual(result["unallocated_power_w"], 30.0)
        for item in result["allocations"]:
            self.assertEqual(item["state"], "powered")
            self.assertAlmostEqual(item["shortfall_power_w"], 0.0)

    def test_allocations_match_input_order(self) -> None:
        result = self.service.allocate_power_budget(base_request())
        self.assertEqual([item["id"] for item in result["allocations"]], ["a", "b"])

    def test_reserve_is_withheld_from_allocation(self) -> None:
        result = self.service.allocate_power_budget(
            base_request(reserve_power_w=50.0)
        )
        self.assertEqual(result["status"], "constrained")
        self.assertAlmostEqual(result["allocated_power_w"], 50.0)
        self.assertAlmostEqual(result["unallocated_power_w"], 0.0)

    def test_higher_priority_wins_minimum_then_demand(self) -> None:
        # 60 W allocatable: b (priority 20) gets min 5 then demand 30,
        # a (priority 10) gets min 10, then 20 of its remaining 30 gap.
        result = self.service.allocate_power_budget(
            base_request(available_power_w=60.0)
        )
        items = self.by_id(result)
        self.assertAlmostEqual(items["b"]["allocated_power_w"], 30.0)
        self.assertEqual(items["b"]["state"], "powered")
        self.assertAlmostEqual(items["a"]["allocated_power_w"], 30.0)
        self.assertAlmostEqual(items["a"]["shortfall_power_w"], 10.0)
        self.assertEqual(items["a"]["state"], "limited")
        self.assertEqual(result["status"], "constrained")

    def test_same_priority_minimums_split_proportionally(self) -> None:
        # 30 W allocatable, tier minimums sum to 40 -> 3:1 split, lower tier shed.
        payload = base_request(
            loads=[
                {"id": "a", "demand_power_w": 40.0, "min_power_w": 30.0, "priority": 5},
                {"id": "b", "demand_power_w": 20.0, "min_power_w": 10.0, "priority": 5},
                {"id": "c", "demand_power_w": 10.0, "min_power_w": 5.0, "priority": 1},
            ],
            available_power_w=30.0,
        )
        result = self.service.allocate_power_budget(payload)
        items = self.by_id(result)
        self.assertAlmostEqual(items["a"]["allocated_power_w"], 22.5)
        self.assertAlmostEqual(items["b"]["allocated_power_w"], 7.5)
        self.assertEqual(items["a"]["state"], "limited")
        self.assertEqual(items["b"]["state"], "limited")
        self.assertAlmostEqual(items["c"]["allocated_power_w"], 0.0)
        self.assertEqual(items["c"]["state"], "shed")
        self.assertAlmostEqual(result["allocated_power_w"], 30.0)
        self.assertAlmostEqual(result["unallocated_power_w"], 0.0)

    def test_same_priority_demand_topup_split_proportionally(self) -> None:
        # Minimums (10 + 10) fit; 20 W left for gaps of 30 and 10 -> 15 + 5.
        payload = base_request(
            loads=[
                {"id": "a", "demand_power_w": 40.0, "min_power_w": 10.0, "priority": 5},
                {"id": "b", "demand_power_w": 20.0, "min_power_w": 10.0, "priority": 5},
            ],
            available_power_w=40.0,
        )
        result = self.service.allocate_power_budget(payload)
        items = self.by_id(result)
        self.assertAlmostEqual(items["a"]["allocated_power_w"], 25.0)
        self.assertAlmostEqual(items["b"]["allocated_power_w"], 15.0)
        self.assertAlmostEqual(items["a"]["shortfall_power_w"], 15.0)
        self.assertAlmostEqual(items["b"]["shortfall_power_w"], 5.0)

    def test_zero_demand_load_is_powered(self) -> None:
        payload = base_request(
            loads=[
                {"id": "z", "demand_power_w": 0.0, "min_power_w": 0.0, "priority": 0},
            ]
        )
        result = self.service.allocate_power_budget(payload)
        item = result["allocations"][0]
        self.assertEqual(item["state"], "powered")
        self.assertAlmostEqual(item["allocated_power_w"], 0.0)
        self.assertEqual(result["status"], "satisfied")
        self.assertAlmostEqual(result["unallocated_power_w"], 100.0)

    def test_zero_available_power_sheds_all_demand(self) -> None:
        result = self.service.allocate_power_budget(
            base_request(available_power_w=0.0)
        )
        items = self.by_id(result)
        self.assertEqual(items["a"]["state"], "shed")
        self.assertEqual(items["b"]["state"], "shed")
        self.assertAlmostEqual(result["allocated_power_w"], 0.0)
        self.assertEqual(result["status"], "constrained")

    def test_no_allocation_exceeds_bounds(self) -> None:
        payload = base_request(
            available_power_w=37.5,
            reserve_power_w=2.5,
            loads=[
                {"id": "a", "demand_power_w": 33.3, "min_power_w": 11.1, "priority": 7},
                {"id": "b", "demand_power_w": 22.2, "min_power_w": 3.3, "priority": 7},
                {"id": "c", "demand_power_w": 10.0, "min_power_w": 0.0, "priority": 3},
            ],
        )
        result = self.service.allocate_power_budget(payload)
        demands = {load["id"]: load["demand_power_w"] for load in payload["loads"]}
        total = 0.0
        for item in result["allocations"]:
            self.assertGreaterEqual(item["allocated_power_w"], 0.0)
            self.assertLessEqual(item["allocated_power_w"], demands[item["id"]])
            total += item["allocated_power_w"]
        self.assertLessEqual(total, 35.0 + 1e-9)
        self.assertAlmostEqual(result["allocated_power_w"], total)

    def test_input_is_not_modified(self) -> None:
        payload = base_request(available_power_w=20.0, reserve_power_w=5.0)
        snapshot = json.loads(json.dumps(payload))
        self.service.allocate_power_budget(payload)
        self.assertEqual(payload, snapshot)

    def test_reordered_loads_give_same_results_by_id(self) -> None:
        loads = [
            {"id": "a", "demand_power_w": 40.0, "min_power_w": 30.0, "priority": 5},
            {"id": "b", "demand_power_w": 20.0, "min_power_w": 10.0, "priority": 5},
            {"id": "c", "demand_power_w": 15.0, "min_power_w": 5.0, "priority": 9},
            {"id": "d", "demand_power_w": 10.0, "min_power_w": 0.0, "priority": 1},
        ]
        forward = self.service.allocate_power_budget(
            base_request(available_power_w=45.0, loads=loads)
        )
        backward = self.service.allocate_power_budget(
            base_request(available_power_w=45.0, loads=list(reversed(loads)))
        )
        forward_by_id = self.by_id(forward)
        for item in backward["allocations"]:
            self.assertAlmostEqual(
                item["allocated_power_w"],
                forward_by_id[item["id"]]["allocated_power_w"],
            )
            self.assertEqual(item["state"], forward_by_id[item["id"]]["state"])


class BudgetValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.allocate_power_budget(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)
        self.assertTrue(ctx.exception.message)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_available_power_rules(self) -> None:
        self.assert_code(base_request(available_power_w=None), "invalid_budget")
        self.assert_code(base_request(available_power_w=True), "invalid_budget")
        self.assert_code(base_request(available_power_w=-1.0), "invalid_budget")
        self.assert_code(base_request(available_power_w="x"), "invalid_budget")
        self.assert_code(
            base_request(available_power_w=float("nan")), "invalid_budget"
        )
        self.assert_code(
            base_request(available_power_w=float("inf")), "invalid_budget"
        )
        payload = base_request()
        del payload["available_power_w"]
        self.assert_code(payload, "invalid_budget")

    def test_reserve_power_rules(self) -> None:
        self.assert_code(base_request(reserve_power_w=None), "invalid_budget")
        self.assert_code(base_request(reserve_power_w=False), "invalid_budget")
        self.assert_code(base_request(reserve_power_w=-0.5), "invalid_budget")
        self.assert_code(
            base_request(reserve_power_w=float("inf")), "invalid_budget"
        )
        self.assert_code(
            base_request(reserve_power_w=100.1), "invalid_budget"
        )

    def test_loads_rules(self) -> None:
        self.assert_code(base_request(loads=[]), "invalid_loads")
        self.assert_code(base_request(loads=None), "invalid_loads")
        self.assert_code(base_request(loads="x"), "invalid_loads")
        self.assert_code(base_request(loads=[1, 2]), "invalid_loads")
        payload = base_request()
        del payload["loads"]
        self.assert_code(payload, "invalid_loads")

    def test_load_id_rules(self) -> None:
        loads = base_request()["loads"]
        missing = [dict(loads[0]), {k: v for k, v in loads[1].items() if k != "id"}]
        self.assert_code(base_request(loads=missing), "invalid_load_id")
        empty = [dict(loads[0]), dict(loads[1], id="")]
        self.assert_code(base_request(loads=empty), "invalid_load_id")
        non_string = [dict(loads[0]), dict(loads[1], id=7)]
        self.assert_code(base_request(loads=non_string), "invalid_load_id")
        duplicate = [dict(loads[0]), dict(loads[1], id="a")]
        self.assert_code(base_request(loads=duplicate), "invalid_load_id")

    def test_load_power_rules(self) -> None:
        loads = base_request()["loads"]
        for bad in (None, True, -1.0, "x", float("nan"), float("inf")):
            self.assert_code(
                base_request(loads=[dict(loads[0], demand_power_w=bad), loads[1]]),
                "invalid_load_power",
            )
            self.assert_code(
                base_request(loads=[dict(loads[0], min_power_w=bad), loads[1]]),
                "invalid_load_power",
            )
        missing_demand = [
            {k: v for k, v in loads[0].items() if k != "demand_power_w"},
            loads[1],
        ]
        self.assert_code(base_request(loads=missing_demand), "invalid_load_power")
        missing_min = [
            {k: v for k, v in loads[0].items() if k != "min_power_w"},
            loads[1],
        ]
        self.assert_code(base_request(loads=missing_min), "invalid_load_power")
        min_above_demand = [dict(loads[0], min_power_w=41.0), loads[1]]
        self.assert_code(
            base_request(loads=min_above_demand), "invalid_load_power"
        )

    def test_priority_rules(self) -> None:
        loads = base_request()["loads"]
        for bad in (None, True, 1.5, "x", -1, 101):
            self.assert_code(
                base_request(loads=[dict(loads[0], priority=bad), loads[1]]),
                "invalid_priority",
            )
        missing = [
            {k: v for k, v in loads[0].items() if k != "priority"},
            loads[1],
        ]
        self.assert_code(base_request(loads=missing), "invalid_priority")

    def test_boundary_priorities_are_accepted(self) -> None:
        loads = base_request()["loads"]
        result = self.service.allocate_power_budget(
            base_request(
                loads=[dict(loads[0], priority=0), dict(loads[1], priority=100)]
            )
        )
        self.assertEqual(result["status"], "satisfied")


class BudgetHttpTest(unittest.TestCase):
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
            self.base + route, data=raw, method="POST"
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read())
            exc.close()
            return exc.code, body

    def test_allocate_route_returns_200(self) -> None:
        status, body = self.post(
            POWER_BUDGET_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "satisfied")
        self.assertEqual(len(body["allocations"]), 2)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(POWER_BUDGET_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(POWER_BUDGET_ROUTE, b"{nope")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_budget_is_422(self) -> None:
        payload = base_request(available_power_w=-1)
        status, body = self.post(POWER_BUDGET_ROUTE, json.dumps(payload).encode())
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_budget")
        self.assertTrue(body["error"]["message"])

    def test_unknown_post_path_is_still_404(self) -> None:
        status, body = self.post("/v1/power/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
