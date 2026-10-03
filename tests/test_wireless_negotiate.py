import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from powerrail.server import WIRELESS_NEGOTIATE_ROUTE, Handler
from powerrail.service import ApiError, Service


def profile(profile_id, voltage, current, accepted=True):
    return {
        "id": profile_id,
        "voltage_v": voltage,
        "max_current_a": current,
        "accepted": accepted,
    }


def config(**overrides):
    cfg = {
        "transmitter_max_power_w": 60.0,
        "receiver_max_power_w": 50.0,
        "recovery_temperature_c": 35.0,
        "warning_temperature_c": 45.0,
        "critical_temperature_c": 60.0,
        "profiles": [
            profile("p5", 5.0, 1.0),
            profile("p10", 10.0, 1.0),
            profile("p20", 20.0, 1.5),
            profile("p40", 20.0, 2.0),
            profile("p60", 20.0, 3.0),
            profile("p100", 25.0, 4.0, accepted=False),
        ],
    }
    cfg.update(overrides)
    return cfg


def sample(
    timestamp,
    requested=10.0,
    present=True,
    coupling=1.0,
    temperature=30.0,
    foreign_object=False,
):
    return {
        "timestamp_s": timestamp,
        "receiver_present": present,
        "requested_power_w": requested,
        "coupling": coupling,
        "coil_temperature_c": temperature,
        "foreign_object": foreign_object,
    }


def base_request(**overrides):
    payload = {
        "config": config(),
        "samples": [
            sample(0, requested=8.0),
            sample(1, requested=45.0),
            sample(2, requested=55.0),
            sample(3, requested=0.0),
            sample(4, requested=10.0, present=False),
        ],
    }
    payload.update(overrides)
    return payload


class WirelessNegotiateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_response_shape_and_states(self) -> None:
        result = self.service.negotiate_wireless_charging(base_request())
        decisions = result["decisions"]
        self.assertEqual(len(decisions), 5)
        self.assertEqual(
            [item["timestamp_s"] for item in decisions], [0, 1, 2, 3, 4]
        )
        for item in decisions:
            self.assertEqual(
                set(item),
                {
                    "timestamp_s",
                    "selected_profile_id",
                    "delivered_power_w",
                    "unmet_power_w",
                    "thermal_factor",
                    "state",
                    "fault_reason",
                },
            )
        self.assertEqual(
            [item["state"] for item in decisions],
            ["charging", "charging", "limited", "idle", "idle"],
        )
        self.assertEqual(result["final_state"], "idle")
        self.assertEqual(result["fault_count"], 0)

    def test_smallest_satisfying_profile_is_selected(self) -> None:
        result = self.service.negotiate_wireless_charging(base_request())
        decisions = result["decisions"]
        # 8 W request: p10 (10 W) is the smallest profile that covers it.
        self.assertEqual(decisions[0]["selected_profile_id"], "p10")
        self.assertAlmostEqual(decisions[0]["delivered_power_w"], 8.0)
        self.assertAlmostEqual(decisions[0]["unmet_power_w"], 0.0)
        # 45 W request: only p60 (capped at the 50 W receiver limit) covers it.
        self.assertEqual(decisions[1]["selected_profile_id"], "p60")
        self.assertAlmostEqual(decisions[1]["delivered_power_w"], 45.0)

    def test_unsatisfiable_request_picks_highest_cap(self) -> None:
        result = self.service.negotiate_wireless_charging(base_request())
        decision = result["decisions"][2]
        # 55 W request exceeds every cap; p60 caps at the 50 W receiver limit.
        self.assertEqual(decision["selected_profile_id"], "p60")
        self.assertAlmostEqual(decision["delivered_power_w"], 50.0)
        self.assertAlmostEqual(decision["unmet_power_w"], 5.0)
        self.assertEqual(decision["state"], "limited")

    def test_zero_request_and_absent_receiver_are_idle(self) -> None:
        result = self.service.negotiate_wireless_charging(base_request())
        decisions = result["decisions"]
        for item in (decisions[3], decisions[4]):
            self.assertEqual(item["state"], "idle")
            self.assertIsNone(item["selected_profile_id"])
            self.assertEqual(item["delivered_power_w"], 0.0)
            self.assertIsNone(item["fault_reason"])
        self.assertEqual(decisions[3]["unmet_power_w"], 0.0)
        self.assertEqual(decisions[4]["unmet_power_w"], 10.0)

    def test_unaccepted_profile_is_never_selected(self) -> None:
        payload = {
            "config": config(
                profiles=[
                    profile("p100", 25.0, 4.0, accepted=False),
                    profile("p10", 10.0, 1.0),
                ]
            ),
            "samples": [sample(0, requested=50.0)],
        }
        result = self.service.negotiate_wireless_charging(payload)
        decision = result["decisions"][0]
        self.assertEqual(decision["selected_profile_id"], "p10")
        self.assertEqual(decision["state"], "limited")

    def test_cap_tie_breaks_by_profile_power_then_id(self) -> None:
        profiles = [
            profile("pb", 20.0, 3.0),  # 60 W, capped at 50 W
            profile("pa", 25.0, 2.0),  # 50 W, capped at 50 W
            profile("pc", 30.0, 2.0),  # 60 W, capped at 50 W
        ]
        payload = {
            "config": config(profiles=profiles),
            "samples": [sample(0, requested=55.0)],
        }
        result = self.service.negotiate_wireless_charging(payload)
        # All caps tie at 50 W; the lowest profile power wins.
        self.assertEqual(result["decisions"][0]["selected_profile_id"], "pa")

        payload["config"] = config(
            profiles=[profile("pb", 20.0, 3.0), profile("pa", 30.0, 2.0)]
        )
        result = self.service.negotiate_wireless_charging(payload)
        # Equal power (60 W) and equal cap: ascending id wins.
        self.assertEqual(result["decisions"][0]["selected_profile_id"], "pa")

    def test_coupling_scales_delivery_cap(self) -> None:
        payload = {
            "config": config(),
            "samples": [sample(0, requested=8.0, coupling=0.5)],
        }
        result = self.service.negotiate_wireless_charging(payload)
        decision = result["decisions"][0]
        # Caps halve: p10 -> 5 W no longer satisfies, p20 -> 15 W does.
        self.assertEqual(decision["selected_profile_id"], "p20")
        self.assertAlmostEqual(decision["delivered_power_w"], 8.0)

    def test_thermal_factor_derates_linearly(self) -> None:
        payload = {
            "config": config(),
            "samples": [
                sample(0, requested=8.0, temperature=45.0),
                sample(1, requested=8.0, temperature=52.5),
                sample(2, requested=8.0, temperature=59.0),
            ],
        }
        result = self.service.negotiate_wireless_charging(payload)
        decisions = result["decisions"]
        self.assertAlmostEqual(decisions[0]["thermal_factor"], 1.0)
        self.assertAlmostEqual(decisions[1]["thermal_factor"], 0.5)
        self.assertAlmostEqual(decisions[2]["thermal_factor"], 1.0 / 15.0)
        # At the midpoint p10 caps at 5 W, so p20 (15 W cap) is selected.
        self.assertEqual(decisions[1]["selected_profile_id"], "p20")
        # Near critical even p60 caps at 50/15 W < 8 W: limited.
        self.assertEqual(decisions[2]["state"], "limited")
        self.assertAlmostEqual(decisions[2]["delivered_power_w"], 50.0 / 15.0)

    def test_foreign_object_latches_until_safe_idle_sample(self) -> None:
        payload = {
            "config": config(),
            "samples": [
                sample(0, requested=8.0),
                sample(1, requested=8.0, foreign_object=True),
                sample(2, requested=8.0),
                sample(3, requested=8.0, present=False, temperature=30.0),
                sample(4, requested=8.0),
            ],
        }
        result = self.service.negotiate_wireless_charging(payload)
        decisions = result["decisions"]
        self.assertEqual(
            [item["state"] for item in decisions],
            ["charging", "fault", "fault", "idle", "charging"],
        )
        self.assertEqual(decisions[1]["fault_reason"], "foreign_object")
        self.assertEqual(decisions[1]["delivered_power_w"], 0.0)
        self.assertIsNone(decisions[1]["selected_profile_id"])
        self.assertEqual(decisions[1]["unmet_power_w"], 8.0)
        # Latch persists after the object clears while the receiver stays.
        self.assertEqual(decisions[2]["fault_reason"], "foreign_object")
        # Receiver absent + no object + cool releases the latch; idle sample.
        self.assertIsNone(decisions[3]["fault_reason"])
        self.assertEqual(result["fault_count"], 1)

    def test_over_temperature_latches_until_recovery_conditions(self) -> None:
        payload = {
            "config": config(),
            "samples": [
                sample(0, requested=8.0, temperature=60.0),
                sample(1, requested=8.0, temperature=50.0),
                sample(2, requested=8.0, present=False, temperature=40.0),
                sample(3, requested=8.0, present=False, temperature=35.0),
                sample(4, requested=8.0, temperature=30.0),
            ],
        }
        result = self.service.negotiate_wireless_charging(payload)
        decisions = result["decisions"]
        self.assertEqual(
            [item["state"] for item in decisions],
            ["fault", "fault", "fault", "idle", "charging"],
        )
        self.assertEqual(decisions[0]["fault_reason"], "over_temperature")
        self.assertEqual(decisions[0]["thermal_factor"], 0.0)
        # Below critical but above recovery: still latched.
        self.assertEqual(decisions[1]["fault_reason"], "over_temperature")
        # Receiver absent but still too warm: no release.
        self.assertEqual(decisions[2]["state"], "fault")
        self.assertEqual(result["fault_count"], 1)

    def test_fault_count_tracks_each_latch_entry(self) -> None:
        payload = {
            "config": config(),
            "samples": [
                sample(0, requested=8.0, foreign_object=True),
                sample(1, requested=8.0, present=False, temperature=30.0),
                sample(2, requested=8.0, temperature=61.0),
                sample(3, requested=8.0, present=False, temperature=30.0),
                sample(4, requested=8.0),
            ],
        }
        result = self.service.negotiate_wireless_charging(payload)
        self.assertEqual(result["fault_count"], 2)
        self.assertEqual(result["final_state"], "charging")

    def test_successful_call_does_not_modify_input(self) -> None:
        payload = base_request()
        snapshot = json.loads(json.dumps(payload))
        self.service.negotiate_wireless_charging(payload)
        self.assertEqual(payload, snapshot)

    def test_timestamp_is_preserved_verbatim(self) -> None:
        payload = {"config": config(), "samples": [sample(7, requested=8.0)]}
        result = self.service.negotiate_wireless_charging(payload)
        self.assertEqual(result["decisions"][0]["timestamp_s"], 7)
        self.assertIsInstance(result["decisions"][0]["timestamp_s"], int)


class WirelessNegotiateValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.negotiate_wireless_charging(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_config_rules(self) -> None:
        good_samples = [sample(0)]
        self.assert_code({"samples": good_samples}, "invalid_wireless_config")
        self.assert_code(
            {"config": None, "samples": good_samples}, "invalid_wireless_config"
        )
        self.assert_code(
            {"config": "x", "samples": good_samples}, "invalid_wireless_config"
        )
        for field in ("transmitter_max_power_w", "receiver_max_power_w"):
            for bad_limit in (None, 0.0, -1.0, float("nan"), float("inf"), True, "50"):
                self.assert_code(
                    {
                        "config": config(**{field: bad_limit}),
                        "samples": good_samples,
                    },
                    "invalid_wireless_config",
                )
        for field in (
            "recovery_temperature_c",
            "warning_temperature_c",
            "critical_temperature_c",
        ):
            for bad_temp in (None, float("nan"), float("inf"), False, "45"):
                self.assert_code(
                    {
                        "config": config(**{field: bad_temp}),
                        "samples": good_samples,
                    },
                    "invalid_wireless_config",
                )

    def test_threshold_ordering_rules(self) -> None:
        good_samples = [sample(0)]
        self.assert_code(
            {
                "config": config(recovery_temperature_c=45.0),
                "samples": good_samples,
            },
            "invalid_wireless_config",
        )
        self.assert_code(
            {
                "config": config(warning_temperature_c=60.0),
                "samples": good_samples,
            },
            "invalid_wireless_config",
        )
        self.assert_code(
            {
                "config": config(critical_temperature_c=35.0),
                "samples": good_samples,
            },
            "invalid_wireless_config",
        )

    def test_profiles_rules(self) -> None:
        good_samples = [sample(0)]
        for bad_profiles in (None, [], "x"):
            self.assert_code(
                {
                    "config": config(profiles=bad_profiles),
                    "samples": good_samples,
                },
                "invalid_profiles",
            )
        self.assert_code(
            {"config": config(profiles=[1]), "samples": good_samples},
            "invalid_profiles",
        )
        # Missing, empty and duplicated ids.
        self.assert_code(
            {
                "config": config(profiles=[profile("", 5.0, 1.0)]),
                "samples": good_samples,
            },
            "invalid_profiles",
        )
        self.assert_code(
            {
                "config": config(
                    profiles=[profile("p", 5.0, 1.0), profile("p", 10.0, 1.0)]
                ),
                "samples": good_samples,
            },
            "invalid_profiles",
        )
        # Non-positive voltage or current.
        for bad_profile in (
            profile("p", 0.0, 1.0),
            profile("p", -5.0, 1.0),
            profile("p", 5.0, 0.0),
            profile("p", 5.0, -1.0),
            profile("p", float("nan"), 1.0),
            profile("p", 5.0, float("inf")),
        ):
            self.assert_code(
                {
                    "config": config(profiles=[bad_profile]),
                    "samples": good_samples,
                },
                "invalid_profiles",
            )
        # Non-boolean accepted flag.
        bad = profile("p", 5.0, 1.0)
        bad["accepted"] = 1
        self.assert_code(
            {"config": config(profiles=[bad]), "samples": good_samples},
            "invalid_profiles",
        )
        # No acceptable profile.
        self.assert_code(
            {
                "config": config(profiles=[profile("p", 5.0, 1.0, accepted=False)]),
                "samples": good_samples,
            },
            "invalid_profiles",
        )

    def test_samples_rules(self) -> None:
        self.assert_code({"config": config()}, "invalid_samples")
        self.assert_code({"config": config(), "samples": []}, "invalid_samples")
        self.assert_code({"config": config(), "samples": None}, "invalid_samples")
        self.assert_code({"config": config(), "samples": "x"}, "invalid_samples")
        self.assert_code({"config": config(), "samples": [1]}, "invalid_samples")

    def test_timestamp_rules(self) -> None:
        self.assert_code(
            {
                "config": config(),
                "samples": [
                    {
                        "receiver_present": True,
                        "requested_power_w": 1.0,
                        "coupling": 1.0,
                        "coil_temperature_c": 30.0,
                        "foreign_object": False,
                    }
                ],
            },
            "invalid_timestamp",
        )
        for bad_timestamp in (float("nan"), float("inf"), True, "1"):
            self.assert_code(
                {"config": config(), "samples": [sample(bad_timestamp)]},
                "invalid_timestamp",
            )
        self.assert_code(
            {"config": config(), "samples": [sample(1), sample(1)]},
            "invalid_timestamp",
        )
        self.assert_code(
            {"config": config(), "samples": [sample(2), sample(1)]},
            "invalid_timestamp",
        )

    def test_sample_field_rules(self) -> None:
        for bad_present in (None, 1, "yes"):
            bad = sample(0)
            bad["receiver_present"] = bad_present
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_wireless_sample"
            )
        for bad_requested in (None, -0.5, float("nan"), float("inf"), True, "5"):
            self.assert_code(
                {"config": config(), "samples": [sample(0, requested=bad_requested)]},
                "invalid_wireless_sample",
            )
        for bad_coupling in (None, -0.1, 1.1, float("nan"), float("inf"), True, "1"):
            self.assert_code(
                {"config": config(), "samples": [sample(0, coupling=bad_coupling)]},
                "invalid_wireless_sample",
            )
        for bad_temperature in (None, float("nan"), float("inf"), False, "30"):
            self.assert_code(
                {
                    "config": config(),
                    "samples": [sample(0, temperature=bad_temperature)],
                },
                "invalid_wireless_sample",
            )
        for bad_object in (None, 0, "no"):
            bad = sample(0)
            bad["foreign_object"] = bad_object
            self.assert_code(
                {"config": config(), "samples": [bad]}, "invalid_wireless_sample"
            )


class WirelessNegotiateHttpTest(unittest.TestCase):
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
            WIRELESS_NEGOTIATE_ROUTE, json.dumps(base_request()).encode()
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["decisions"]), 5)
        self.assertEqual(body["final_state"], "idle")
        self.assertEqual(body["fault_count"], 0)

    def test_missing_body_is_invalid_json(self) -> None:
        status, body = self.post(WIRELESS_NEGOTIATE_ROUTE, b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_malformed_json_is_invalid_json(self) -> None:
        status, body = self.post(WIRELESS_NEGOTIATE_ROUTE, b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_json_non_object_is_invalid_json(self) -> None:
        status, body = self.post(WIRELESS_NEGOTIATE_ROUTE, b"[1, 2]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_invalid_config_is_422(self) -> None:
        status, body = self.post(
            WIRELESS_NEGOTIATE_ROUTE,
            json.dumps({"config": {}, "samples": [sample(0)]}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_wireless_config")

    def test_invalid_profiles_is_422(self) -> None:
        status, body = self.post(
            WIRELESS_NEGOTIATE_ROUTE,
            json.dumps(
                {"config": config(profiles=[]), "samples": [sample(0)]}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_profiles")

    def test_invalid_samples_is_422(self) -> None:
        status, body = self.post(
            WIRELESS_NEGOTIATE_ROUTE,
            json.dumps({"config": config(), "samples": []}).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_samples")

    def test_invalid_timestamp_is_422(self) -> None:
        status, body = self.post(
            WIRELESS_NEGOTIATE_ROUTE,
            json.dumps(
                {"config": config(), "samples": [sample(1), sample(1)]}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_timestamp")

    def test_invalid_wireless_sample_is_422(self) -> None:
        status, body = self.post(
            WIRELESS_NEGOTIATE_ROUTE,
            json.dumps(
                {"config": config(), "samples": [sample(0, coupling=2.0)]}
            ).encode(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_wireless_sample")

    def test_unknown_path_is_404(self) -> None:
        status, body = self.post("/v1/power/wireless/unknown", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
