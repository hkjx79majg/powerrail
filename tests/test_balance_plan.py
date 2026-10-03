import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import BALANCE_PLAN_ROUTE, Handler
from powerrail.service import ApiError, Service


def cell(cell_id, voltage, temperature=25.0):
    return {"id": cell_id, "voltage_v": voltage, "temperature_c": temperature}


def config(**overrides):
    values = {
        "start_delta_v": 0.05,
        "stop_delta_v": 0.01,
        "bleed_current_a": 1.0,
        "max_temperature_c": 45.0,
        "max_channels": 2,
    }
    values.update(overrides)
    return values


def base_request(**overrides):
    payload = {
        "cells": [
            cell("c1", 3.20),
            cell("c2", 3.30),
            cell("c3", 3.26),
            cell("c4", 3.22),
        ],
        "config": config(),
    }
    payload.update(overrides)
    return payload


class BalancePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_target_voltage_uses_minimum(self) -> None:
        result = self.service.plan_balance(base_request())
        self.assertEqual(result["target_voltage_v"], 3.20)
        self.assertEqual(result["status"], "balancing")

    def test_decisions_match_input_order_and_shape(self) -> None:
        result = self.service.plan_balance(base_request())
        decisions = result["decisions"]
        self.assertEqual([item["id"] for item in decisions], ["c1", "c2", "c3", "c4"])
        for item in decisions:
            self.assertEqual(
                set(item),
                {"id", "delta_voltage_v", "active", "bleed_current_a", "reason"},
            )

    def test_candidates_sorted_by_delta_desc_id_asc(self) -> None:
        result = self.service.plan_balance(base_request())
        # deltas: c1=0.00 below, c2=0.10, c3=0.06, c4=0.02 below start
        # max_channels=2 -> c2 and c3 selected
        by_id = {item["id"]: item for item in result["decisions"]}
        self.assertTrue(by_id["c2"]["active"])
        self.assertEqual(by_id["c2"]["bleed_current_a"], 1.0)
        self.assertEqual(by_id["c2"]["reason"], "selected")
        self.assertTrue(by_id["c3"]["active"])
        self.assertEqual(by_id["c3"]["reason"], "selected")
        self.assertEqual(result["active_ids"], ["c2", "c3"])
        self.assertFalse(by_id["c1"]["active"])
        self.assertEqual(by_id["c1"]["bleed_current_a"], 0.0)
        self.assertEqual(by_id["c1"]["reason"], "below_threshold")
        self.assertFalse(by_id["c4"]["active"])
        self.assertEqual(by_id["c4"]["reason"], "below_threshold")

    def test_tie_break_uses_ascending_id(self) -> None:
        payload = {
            "cells": [
                cell("b", 3.30),
                cell("a", 3.30),
                cell("m", 3.20),
            ],
            "config": config(max_channels=2),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["active_ids"], ["a", "b"])

    def test_start_threshold_is_inclusive_for_inactive(self) -> None:
        payload = {
            "cells": [cell("lo", 3.0), cell("hi", 3.25)],
            "config": config(start_delta_v=0.25, max_channels=1),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["decisions"][1]["reason"], "selected")

    def test_stop_threshold_is_strict_for_previously_active(self) -> None:
        # delta exactly at stop_delta_v stops balancing
        payload = {
            "cells": [cell("lo", 3.0), cell("hi", 3.125)],
            "config": config(stop_delta_v=0.125, start_delta_v=0.25, max_channels=1),
            "previous_active_ids": ["hi"],
        }
        result = self.service.plan_balance(payload)
        self.assertFalse(result["decisions"][1]["active"])
        self.assertEqual(result["decisions"][1]["reason"], "below_threshold")
        self.assertEqual(result["status"], "idle")

        # delta strictly above stop keeps balancing even below start
        payload["cells"][1]["voltage_v"] = 3.25
        result = self.service.plan_balance(payload)
        self.assertTrue(result["decisions"][1]["active"])
        self.assertEqual(result["decisions"][1]["reason"], "selected")

    def test_inactive_between_thresholds_does_not_start(self) -> None:
        payload = {
            "cells": [cell("lo", 3.0), cell("hi", 3.125)],
            "config": config(start_delta_v=0.25, stop_delta_v=0.125, max_channels=1),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["decisions"][1]["reason"], "below_threshold")

    def test_max_temperature_blocks_balancing(self) -> None:
        payload = {
            "cells": [cell("lo", 3.20, 30.0), cell("hi", 3.30, 45.0)],
            "config": config(max_channels=2),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["decisions"][1]["reason"], "temperature_blocked")
        self.assertFalse(result["decisions"][1]["active"])
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["active_ids"], [])

    def test_previously_active_still_blocked_by_temperature(self) -> None:
        payload = {
            "cells": [cell("lo", 3.20, 30.0), cell("hi", 3.30, 50.0)],
            "config": config(max_channels=2),
            "previous_active_ids": ["hi"],
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["decisions"][1]["reason"], "temperature_blocked")

    def test_channel_limited_candidate_stays_inactive(self) -> None:
        payload = {
            "cells": [
                cell("lo", 3.20),
                cell("a", 3.30),
                cell("b", 3.29),
                cell("c", 3.28),
            ],
            "config": config(max_channels=2),
        }
        result = self.service.plan_balance(payload)
        by_id = {item["id"]: item for item in result["decisions"]}
        self.assertEqual(by_id["a"]["reason"], "selected")
        self.assertEqual(by_id["b"]["reason"], "selected")
        self.assertEqual(by_id["c"]["reason"], "channel_limited")
        self.assertFalse(by_id["c"]["active"])
        self.assertEqual(by_id["c"]["bleed_current_a"], 0.0)

    def test_channel_limited_after_hysteresis_priority(self) -> None:
        # A previously active low-delta cell loses a channel to a higher-delta
        # inactive candidate: selection is purely by delta ordering.
        payload = {
            "cells": [
                cell("lo", 3.20),
                cell("old", 3.22),
                cell("new", 3.30),
            ],
            "config": config(
                start_delta_v=0.05, stop_delta_v=0.01, max_channels=1
            ),
            "previous_active_ids": ["old"],
        }
        result = self.service.plan_balance(payload)
        by_id = {item["id"]: item for item in result["decisions"]}
        self.assertEqual(by_id["new"]["reason"], "selected")
        self.assertEqual(by_id["old"]["reason"], "channel_limited")

    def test_minimum_cell_never_balances(self) -> None:
        payload = {
            "cells": [cell("lo", 3.20), cell("hi", 3.30)],
            "config": config(max_channels=2),
            "previous_active_ids": ["lo"],
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["decisions"][0]["reason"], "below_threshold")
        self.assertEqual(result["decisions"][0]["delta_voltage_v"], 0.0)

    def test_status_idle_when_nothing_selected(self) -> None:
        payload = {
            "cells": [cell("lo", 3.20), cell("hi", 3.21)],
            "config": config(),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["active_ids"], [])

    def test_omitted_previous_active_ids_treated_as_empty(self) -> None:
        result = self.service.plan_balance(base_request())
        self.assertNotIn("previous_active_ids", result)

    def test_max_channels_equal_cell_count(self) -> None:
        payload = {
            "cells": [cell("lo", 3.20), cell("hi", 3.29)],
            "config": config(max_channels=2),
        }
        result = self.service.plan_balance(payload)
        self.assertEqual(result["active_ids"], ["hi"])

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request(previous_active_ids=["c2"])
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
            {"cells": [1], "config": config()},
            "invalid_cells",
        )

    def test_cell_id_rules(self) -> None:
        for bad_id in (None, "", 3):
            self.assert_code(
                {"cells": [cell(bad_id, 3.2)],
                 "config": config(max_channels=1)},
                "invalid_cell_id",
            )
        self.assert_code(
            {"cells": [cell("x", 3.2), cell("x", 3.3)],
             "config": config(max_channels=2)},
            "invalid_cell_id",
        )

    def test_cell_voltage_rules(self) -> None:
        for bad_voltage in (None, 0.0, -1.0, float("nan"), float("inf"), True, "3.2"):
            self.assert_code(
                {"cells": [cell("a", bad_voltage)], "config": config(max_channels=1)},
                "invalid_cell_voltage",
            )

    def test_cell_temperature_rules(self) -> None:
        for bad_temperature in (None, float("nan"), float("inf"), False, "25"):
            self.assert_code(
                {"cells": [cell("a", 3.2, bad_temperature)],
                 "config": config(max_channels=1)},
                "invalid_cell_temperature",
            )

    def test_config_rules(self) -> None:
        good_cells = [cell("a", 3.2)]
        self.assert_code({"cells": good_cells}, "invalid_balance_config")
        self.assert_code(
            {"cells": good_cells, "config": None}, "invalid_balance_config"
        )
        self.assert_code(
            {"cells": good_cells, "config": "x"}, "invalid_balance_config"
        )

        for bad_value in (None, 0.0, -1.0, float("nan"), float("inf"), True, "0.05"):
            self.assert_code(
                {"cells": good_cells,
                 "config": config(max_channels=1, start_delta_v=bad_value)},
                "invalid_balance_config",
            )
            self.assert_code(
                {"cells": good_cells,
                 "config": config(max_channels=1, bleed_current_a=bad_value)},
                "invalid_balance_config",
            )

        for bad_stop in (-0.1, 0.05, 0.06, float("nan"), True, "0.01"):
            self.assert_code(
                {"cells": good_cells,
                 "config": config(max_channels=1, stop_delta_v=bad_stop)},
                "invalid_balance_config",
            )

        # stop_delta_v == 0 is allowed
        result = self.service.plan_balance(
            {"cells": [cell("a", 3.2)], "config": config(
                max_channels=1, stop_delta_v=0.0
            )}
        )
        self.assertEqual(result["status"], "idle")

        for bad_temp in (None, float("nan"), float("inf"), False, "45"):
            self.assert_code(
                {"cells": good_cells,
                 "config": config(max_channels=1, max_temperature_c=bad_temp)},
                "invalid_balance_config",
            )

        for bad_channels in (None, 0, -1, 2, True, 1.0, "2"):
            self.assert_code(
                {"cells": good_cells,
                 "config": config(max_channels=bad_channels)},
                "invalid_balance_config",
            )

    def test_previous_active_ids_rules(self) -> None:
        good_cells = [cell("a", 3.2), cell("b", 3.3)]
        for bad_value in (None, "a", 3, {"id": "a"}):
            self.assert_code(
                {
                    "cells": good_cells,
                    "config": config(max_channels=2),
                    "previous_active_ids": bad_value,
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
                "previous_active_ids": ["a", "z"],
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
                "previous_active_ids": [""],
            },
            "invalid_previous_active_ids",
        )

    def test_empty_previous_active_ids_is_valid(self) -> None:
        result = self.service.plan_balance(
            {
                "cells": [cell("a", 3.2), cell("b", 3.3)],
                "config": config(max_channels=2),
                "previous_active_ids": [],
            }
        )
        self.assertEqual(result["active_ids"], ["b"])


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
        self.assertEqual(body["active_ids"], ["c2", "c3"])
        self.assertEqual(body["status"], "balancing")

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
        payload = {"cells": [cell("", 3.2)], "config": config(max_channels=1)}
        status, body = self.post(
            BALANCE_PLAN_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_cell_id")

    def test_invalid_cell_voltage_is_422(self) -> None:
        payload = {"cells": [cell("a", -1.0)], "config": config(max_channels=1)}
        status, body = self.post(
            BALANCE_PLAN_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_cell_voltage")

    def test_invalid_cell_temperature_is_422(self) -> None:
        payload = {"cells": [cell("a", 3.2, "hot")], "config": config(max_channels=1)}
        status, body = self.post(
            BALANCE_PLAN_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_cell_temperature")

    def test_invalid_config_is_422(self) -> None:
        status, body = self.post(
            BALANCE_PLAN_ROUTE,
            json.dumps({"cells": [cell("a", 3.2)], "config": {}}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_balance_config")

    def test_invalid_previous_active_ids_is_422(self) -> None:
        payload = {
            "cells": [cell("a", 3.2)],
            "config": config(max_channels=1),
            "previous_active_ids": ["z"],
        }
        status, body = self.post(
            BALANCE_PLAN_ROUTE, json.dumps(payload).encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_previous_active_ids")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/battery/balance/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
