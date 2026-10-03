import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import BALANCE_PLAN_ROUTE, Handler
from powerrail.service import ApiError, Service


def cell(cell_id, voltage, temperature=25.0):
    return {
        "id": cell_id,
        "voltage_v": voltage,
        "temperature_c": temperature,
    }


def config(**overrides):
    values = {
        "start_delta_v": 0.05,
        "stop_delta_v": 0.02,
        "bleed_current_a": 0.5,
        "max_temperature_c": 45.0,
        "max_channels": 4,
    }
    values.update(overrides)
    return values


def base_request(**overrides):
    payload = {
        "cells": [
            cell("c1", 3.30),
            cell("c2", 3.20),
            cell("c3", 3.36),
            cell("c4", 3.22),
        ],
        "config": config(),
    }
    payload.update(overrides)
    return payload


class BalancePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_response_shape_and_basic_selection(self) -> None:
        result = self.service.plan_balance(base_request())
        self.assertEqual(result["target_voltage_v"], 3.20)
        self.assertEqual(result["status"], "balancing")
        decisions = result["decisions"]
        self.assertEqual(
            [item["id"] for item in decisions], ["c1", "c2", "c3", "c4"]
        )
        for item in decisions:
            self.assertEqual(
                set(item),
                {"id", "delta_voltage_v", "active", "bleed_current_a", "reason"},
            )
        deltas = [item["delta_voltage_v"] for item in decisions]
        for actual, expected in zip(deltas, [0.10, 0.0, 0.16, 0.02]):
            self.assertAlmostEqual(actual, expected, places=12)
        self.assertEqual(
            [item["active"] for item in decisions], [True, False, True, False]
        )
        self.assertEqual(
            [item["bleed_current_a"] for item in decisions],
            [0.5, 0.0, 0.5, 0.0],
        )
        self.assertEqual(
            [item["reason"] for item in decisions],
            ["selected", "below_threshold", "selected", "below_threshold"],
        )
        self.assertEqual(result["active_ids"], ["c3", "c1"])

    def test_start_threshold_is_inclusive(self) -> None:
        # Exact binary fractions: the gap equals start_delta_v exactly.
        payload = {
            "cells": [cell("lo", 3.0), cell("hi", 3.5)],
            "config": config(start_delta_v=0.5, max_channels=2),
        }
        result = self.service.plan_balance(payload)
        self.assertTrue(result["decisions"][1]["active"])
        self.assertEqual(result["decisions"][1]["reason"], "selected")

    def test_previously_active_uses_stop_threshold_with_hysteresis(self) -> None:
        # Use exact binary fractions so the threshold boundaries compare exactly.
        payload = {
            "cells": [
                cell("lo", 3.0),
                cell("keep", 3.375),  # gap 0.375, between stop and start
                cell("drop", 3.25),   # gap 0.25, exactly stop: must stop
                cell("fresh", 3.375),  # inactive, gap below start: stays off
            ],
            "config": config(start_delta_v=0.5, stop_delta_v=0.25),
            "previous_active_ids": ["keep", "drop"],
        }
        result = self.service.plan_balance(payload)
        reasons = {item["id"]: item["reason"] for item in result["decisions"]}
        self.assertEqual(reasons["keep"], "selected")
        self.assertEqual(reasons["drop"], "below_threshold")
        self.assertEqual(reasons["fresh"], "below_threshold")
        self.assertEqual(result["active_ids"], ["keep"])

    def test_stop_threshold_boundary_is_strict(self) -> None:
        payload = {
            "cells": [cell("lo", 3.0), cell("a", 3.375)],
            "config": config(start_delta_v=0.5, stop_delta_v=0.25, max_channels=2),
            "previous_active_ids": ["a"],
        }
        result = self.service.plan_balance(payload)
        self.assertTrue(result["decisions"][1]["active"])
        # An active cell exactly at the stop gap must stop.
        payload = {
            "cells": [cell("lo", 3.0), cell("a", 3.25)],
            "config": config(start_delta_v=0.5, stop_delta_v=0.25, max_channels=2),
            "previous_active_ids": ["a"],
        }
        result = self.service.plan_balance(payload)
        self.assertFalse(result["decisions"][1]["active"])
        self.assertEqual(result["decisions"][1]["reason"], "below_threshold")
        # An active cell at a gap of zero stops immediately.
        payload = {
            "cells": [cell("lo", 3.00), cell("a", 3.00)],
            "config": config(start_delta_v=0.05, stop_delta_v=0.0, max_channels=2),
            "previous_active_ids": ["a"],
        }
        result = self.service.plan_balance(payload)
        self.assertFalse(result["decisions"][1]["active"])
        self.assertEqual(result["decisions"][1]["reason"], "below_threshold")

    def test_temperature_at_limit_blocks_balancing(self) -> None:
        payload = {
            "cells": [
                cell("lo", 3.00, 20.0),
                cell("hot", 3.30, 45.0),
                cell("cool", 3.20, 44.999),
            ],
            "config": config(max_channels=3),
            "previous_active_ids": ["hot"],
        }
        result = self.service.plan_balance(payload)
        by_id = {item["id"]: item for item in result["decisions"]}
        self.assertFalse(by_id["hot"]["active"])
        self.assertEqual(by_id["hot"]["reason"], "temperature_blocked")
        self.assertEqual(by_id["hot"]["bleed_current_a"], 0.0)
        self.assertTrue(by_id["cool"]["active"])
        self.assertEqual(result["active_ids"], ["cool"])

    def test_candidates_ranked_by_delta_desc_id_asc(self) -> None:
        payload = {
            "cells": [
                cell("lo", 3.00),
                cell("z", 3.10),
                cell("y", 3.10),
                cell("x", 3.10),
                cell("q", 3.20),
            ],
            "config": config(max_channels=3),
        }
        result = self.service.plan_balance(payload)
        # q has the largest gap; the 3-way tie is resolved id-ascending: x, y.
        self.assertEqual(result["active_ids"], ["q", "x", "y"])
        reasons = {item["id"]: item["reason"] for item in result["decisions"]}
        self.assertEqual(reasons["z"], "channel_limited")
        self.assertEqual(reasons["lo"], "below_threshold")

    def test_blocked_cells_do_not_consume_channels(self) -> None:
        payload = {
            "cells": [
                cell("lo", 3.00, 25.0),
                cell("hot", 3.50, 50.0),
                cell("a", 3.10, 25.0),
            ],
            "config": config(max_channels=1),
        }
        result = self.service.plan_balance(payload)
        # Only "a" is an eligible candidate, so the single channel goes to it.
        self.assertEqual(result["active_ids"], ["a"])
        reasons = {item["id"]: item["reason"] for item in result["decisions"]}
        self.assertEqual(reasons["hot"], "temperature_blocked")

    def test_idle_when_nothing_selected(self) -> None:
        payload = {
            "cells": [cell("a", 3.00), cell("b", 3.01)],
            "config": config(start_delta_v=0.05, max_channels=2),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["active_ids"], [])
        self.assertFalse(any(item["active"] for item in result["decisions"]))
        self.assertTrue(
            all(item["bleed_current_a"] == 0.0 for item in result["decisions"])
        )

    def test_previous_active_ids_defaults_to_empty(self) -> None:
        payload = {
            "cells": [cell("lo", 3.00), cell("a", 3.03)],
            # Gap is between stop (0.02) and start (0.05), so without prior
            # activation the cell must not start.
            "config": config(start_delta_v=0.05, stop_delta_v=0.02, max_channels=2),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["status"], "idle")

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request(previous_active_ids=["c3"])
        snapshot = json.loads(json.dumps(payload))
        self.service.plan_balance(payload)
        self.assertEqual(payload, snapshot)


class BalancePlanValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.plan_balance(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_cells_rules(self) -> None:
        self.assert_code({"config": config()}, "invalid_cells")
        self.assert_code({"cells": [], "config": config()}, "invalid_cells")
        self.assert_code({"cells": None, "config": config()}, "invalid_cells")
        self.assert_code({"cells": "x", "config": config()}, "invalid_cells")
        self.assert_code(
            {"cells": [cell("a", 3.0), 1], "config": config()}, "invalid_cells"
        )

    def test_cell_id_rules(self) -> None:
        for bad_id in (None, "", 3):
            self.assert_code(
                {
                    "cells": [cell("ok", 3.0), cell(bad_id, 3.1)],
                    "config": config(),
                },
                "invalid_cell_id",
            )
        self.assert_code(
            {"cells": [cell("dup", 3.0), cell("dup", 3.1)], "config": config()},
            "invalid_cell_id",
        )

    def test_cell_voltage_rules(self) -> None:
        for bad_voltage in (None, 0.0, -1.0, float("nan"), float("inf"), True, "3"):
            self.assert_code(
                {
                    "cells": [cell("ok", 3.0), cell("bad", bad_voltage)],
                    "config": config(),
                },
                "invalid_cell_voltage",
            )

    def test_cell_temperature_rules(self) -> None:
        for bad_temperature in (None, float("nan"), float("inf"), False, "25"):
            self.assert_code(
                {
                    "cells": [cell("ok", 3.0), cell("bad", 3.1, bad_temperature)],
                    "config": config(),
                },
                "invalid_cell_temperature",
            )

    def test_config_rules(self) -> None:
        good_cells = [cell("a", 3.0), cell("b", 3.1)]
        valid_config = config(max_channels=2)
        self.assert_code({"cells": good_cells}, "invalid_balance_config")
        self.assert_code(
            {"cells": good_cells, "config": None}, "invalid_balance_config"
        )
        self.assert_code(
            {"cells": good_cells, "config": "x"}, "invalid_balance_config"
        )
        self.assert_code(
            {"cells": good_cells, "config": {}}, "invalid_balance_config"
        )
        for bad_value in (None, 0.0, -1.0, float("nan"), float("inf"), True, "1"):
            self.assert_code(
                {
                    "cells": good_cells,
                    "config": config(max_channels=2, start_delta_v=bad_value),
                },
                "invalid_balance_config",
            )
            self.assert_code(
                {
                    "cells": good_cells,
                    "config": config(max_channels=2, bleed_current_a=bad_value),
                },
                "invalid_balance_config",
            )
        for bad_stop in (None, -0.01, float("nan"), float("inf"), False, "1"):
            self.assert_code(
                {
                    "cells": good_cells,
                    "config": config(max_channels=2, stop_delta_v=bad_stop),
                },
                "invalid_balance_config",
            )
        # stop must be strictly below start.
        self.assert_code(
            {
                "cells": good_cells,
                "config": config(max_channels=2, start_delta_v=0.05, stop_delta_v=0.05),
            },
            "invalid_balance_config",
        )
        self.assert_code(
            {
                "cells": good_cells,
                "config": config(max_channels=2, start_delta_v=0.05, stop_delta_v=0.06),
            },
            "invalid_balance_config",
        )
        for bad_temperature in (None, float("nan"), float("inf"), True, "45"):
            self.assert_code(
                {
                    "cells": good_cells,
                    "config": config(max_channels=2, max_temperature_c=bad_temperature),
                },
                "invalid_balance_config",
            )
        for bad_channels in (None, 0, 3, -1, True, 2.0, "2"):
            self.assert_code(
                {"cells": good_cells, "config": config(max_channels=bad_channels)},
                "invalid_balance_config",
            )

    def test_max_channels_equal_to_cell_count_is_valid(self) -> None:
        payload = {
            "cells": [cell("a", 3.00), cell("b", 3.10)],
            "config": config(max_channels=2),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["active_ids"], ["b"])

    def test_previous_active_ids_rules(self) -> None:
        good_cells = [cell("a", 3.0), cell("b", 3.1)]
        valid_config = config(max_channels=2)
        self.assert_code(
            {
                "cells": good_cells,
                "config": valid_config,
                "previous_active_ids": "a",
            },
            "invalid_previous_active_ids",
        )
        self.assert_code(
            {
                "cells": good_cells,
                "config": config(max_channels=2),
                "previous_active_ids": [1],
            },
            "invalid_previous_active_ids",
        )
        self.assert_code(
            {
                "cells": good_cells,
                "config": config(max_channels=2),
                "previous_active_ids": ["a", "a"],
            },
            "invalid_previous_active_ids",
        )
        self.assert_code(
            {
                "cells": good_cells,
                "config": config(max_channels=2),
                "previous_active_ids": ["a", "missing"],
            },
            "invalid_previous_active_ids",
        )


class BalancePlanHttpTest(unittest.TestCase):
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
            BALANCE_PLAN_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["target_voltage_v"], 3.20)
        self.assertEqual(body["active_ids"], ["c3", "c1"])
        self.assertEqual(body["status"], "balancing")
        self.assertEqual(len(body["decisions"]), 4)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(BALANCE_PLAN_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(BALANCE_PLAN_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(BALANCE_PLAN_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_cells_is_422(self) -> None:
        status, body = self.post(
            BALANCE_PLAN_ROUTE,
            json.dumps({"cells": [], "config": config()}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_cells")

    def test_invalid_cell_id_is_422(self) -> None:
        status, body = self.post(
            BALANCE_PLAN_ROUTE,
            json.dumps(
                {"cells": [cell("a", 3.0), cell("a", 3.1)], "config": config()}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_cell_id")

    def test_invalid_cell_voltage_is_422(self) -> None:
        status, body = self.post(
            BALANCE_PLAN_ROUTE,
            json.dumps(
                {"cells": [cell("a", -3.0)], "config": config()}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_cell_voltage")

    def test_invalid_cell_temperature_is_422(self) -> None:
        status, body = self.post(
            BALANCE_PLAN_ROUTE,
            json.dumps(
                {"cells": [cell("a", 3.0, "hot")], "config": config()}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_cell_temperature")

    def test_invalid_config_is_422(self) -> None:
        status, body = self.post(
            BALANCE_PLAN_ROUTE,
            json.dumps({"cells": [cell("a", 3.0)]}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_balance_config")

    def test_invalid_previous_active_ids_is_422(self) -> None:
        status, body = self.post(
            BALANCE_PLAN_ROUTE,
            json.dumps(
                {
                    "cells": [cell("a", 3.0)],
                    "config": config(max_channels=1),
                    "previous_active_ids": ["ghost"],
                }
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_previous_active_ids")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/balance/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
