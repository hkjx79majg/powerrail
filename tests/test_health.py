import copy
import unittest

from powerrail.service import ApiError, Service


def base_request(**overrides):
    payload = {
        "nominal_capacity_ah": 10.0,
        "measurements": [
            {"timestamp_s": 0, "measured_capacity_ah": 10.0, "cumulative_discharge_ah": 0.0},
            {"timestamp_s": 100, "measured_capacity_ah": 9.5, "cumulative_discharge_ah": 50.0},
        ],
    }
    payload.update(overrides)
    return payload


class HealthEstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_per_item_soh_and_equivalent_cycles(self) -> None:
        result = self.service.estimate_health(base_request())
        estimates = result["estimates"]
        self.assertEqual(len(estimates), 2)
        self.assertEqual(estimates[0]["timestamp_s"], 0)
        self.assertAlmostEqual(estimates[0]["soh"], 1.0)
        self.assertAlmostEqual(estimates[0]["equivalent_cycles"], 0.0)
        self.assertEqual(estimates[1]["timestamp_s"], 100)
        self.assertAlmostEqual(estimates[1]["soh"], 0.95)
        self.assertAlmostEqual(estimates[1]["equivalent_cycles"], 5.0)
        self.assertAlmostEqual(result["latest_soh"], 0.95)
        self.assertAlmostEqual(result["consumed_cycles"], 5.0)

    def test_soh_is_clamped_to_unit_range(self) -> None:
        payload = base_request(
            measurements=[
                {"timestamp_s": 0, "measured_capacity_ah": 12.0, "cumulative_discharge_ah": 0.0},
                {"timestamp_s": 1, "measured_capacity_ah": 0.01, "cumulative_discharge_ah": 1.0},
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["estimates"][0]["soh"], 1.0)

    def test_projection_uses_first_last_degradation_rate(self) -> None:
        payload = base_request(
            measurements=[
                {"timestamp_s": 0, "measured_capacity_ah": 10.0, "cumulative_discharge_ah": 0.0},
                {"timestamp_s": 50, "measured_capacity_ah": 10.2, "cumulative_discharge_ah": 25.0},
                {"timestamp_s": 100, "measured_capacity_ah": 9.0, "cumulative_discharge_ah": 100.0},
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "projected")
        # first/last span: 10 cycles, SoH drop 0.1 -> rate 0.01/cycle;
        # remaining from 0.9 to threshold 0.8 at that rate -> 10 cycles
        self.assertAlmostEqual(result["remaining_cycles"], 10.0)

    def test_default_threshold_is_0_8(self) -> None:
        result = self.service.estimate_health(base_request())
        # latest 0.95 > 0.8, positive cycle span and SoH drop -> projected
        self.assertEqual(result["projection_status"], "projected")
        self.assertIsNotNone(result["remaining_cycles"])

    def test_end_of_life_when_latest_at_or_below_threshold(self) -> None:
        payload = base_request(
            end_of_life_soh=0.95,
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "end_of_life")
        self.assertEqual(result["remaining_cycles"], 0)

    def test_insufficient_trend_without_cycle_span(self) -> None:
        payload = base_request(
            measurements=[
                {"timestamp_s": 0, "measured_capacity_ah": 10.0, "cumulative_discharge_ah": 10.0},
                {"timestamp_s": 1, "measured_capacity_ah": 9.0, "cumulative_discharge_ah": 10.0},
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "insufficient_trend")
        self.assertIsNone(result["remaining_cycles"])

    def test_insufficient_trend_without_soh_drop(self) -> None:
        payload = base_request(
            measurements=[
                {"timestamp_s": 0, "measured_capacity_ah": 9.0, "cumulative_discharge_ah": 0.0},
                {"timestamp_s": 1, "measured_capacity_ah": 9.5, "cumulative_discharge_ah": 10.0},
            ]
        )
        result = self.service.estimate_health(payload)
        self.assertEqual(result["projection_status"], "insufficient_trend")
        self.assertIsNone(result["remaining_cycles"])

    def test_input_is_not_modified(self) -> None:
        payload = base_request()
        snapshot = copy.deepcopy(payload)
        self.service.estimate_health(payload)
        self.assertEqual(payload, snapshot)


class HealthValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_health(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_nominal_capacity_rules(self) -> None:
        self.assert_code(base_request(nominal_capacity_ah=None), "invalid_nominal_capacity")
        self.assert_code(base_request(nominal_capacity_ah=0), "invalid_nominal_capacity")
        self.assert_code(base_request(nominal_capacity_ah=-1), "invalid_nominal_capacity")
        self.assert_code(base_request(nominal_capacity_ah=float("nan")), "invalid_nominal_capacity")
        self.assert_code(
            {k: v for k, v in base_request().items() if k != "nominal_capacity_ah"},
            "invalid_nominal_capacity",
        )

    def test_measurements_rules(self) -> None:
        self.assert_code(base_request(measurements=[]), "invalid_measurements")
        self.assert_code(base_request(measurements="x"), "invalid_measurements")
        self.assert_code(base_request(measurements=None), "invalid_measurements")
        single = [
            {"timestamp_s": 0, "measured_capacity_ah": 10.0, "cumulative_discharge_ah": 0.0},
        ]
        self.assert_code(base_request(measurements=single), "invalid_measurements")
        self.assert_code(base_request(measurements=[{}, {}]), "invalid_timestamp")
        self.assert_code(base_request(measurements=["x", {}]), "invalid_measurements")

    def test_timestamp_rules(self) -> None:
        good = {"measured_capacity_ah": 9.0, "cumulative_discharge_ah": 1.0}
        self.assert_code(
            base_request(measurements=[{"timestamp_s": 0, **good}, {"timestamp_s": 0, **good}]),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(measurements=[{"timestamp_s": 1, **good}, {"timestamp_s": 0, **good}]),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(measurements=[{"timestamp_s": "x", **good}, {"timestamp_s": 1, **good}]),
            "invalid_timestamp",
        )
        self.assert_code(
            base_request(measurements=[good, {"timestamp_s": 1, **good}]),
            "invalid_timestamp",
        )

    def test_capacity_measurement_rules(self) -> None:
        def item(t, c, d=1.0):
            return {"timestamp_s": t, "measured_capacity_ah": c, "cumulative_discharge_ah": d}

        self.assert_code(base_request(measurements=[item(0, 0), item(1, 9.0)]), "invalid_capacity_measurement")
        self.assert_code(base_request(measurements=[item(0, -1), item(1, 9.0)]), "invalid_capacity_measurement")
        self.assert_code(
            base_request(measurements=[item(0, float("inf")), item(1, 9.0)]),
            "invalid_capacity_measurement",
        )
        self.assert_code(
            base_request(
                measurements=[
                    {"timestamp_s": 0, "cumulative_discharge_ah": 0.0},
                    item(1, 9.0),
                ]
            ),
            "invalid_capacity_measurement",
        )

    def test_throughput_rules(self) -> None:
        def item(t, d, c=9.0):
            return {"timestamp_s": t, "measured_capacity_ah": c, "cumulative_discharge_ah": d}

        self.assert_code(
            base_request(measurements=[item(0, -0.01), item(1, 1.0)]), "invalid_throughput"
        )
        self.assert_code(
            base_request(measurements=[item(0, 2.0), item(1, 1.0)]), "invalid_throughput"
        )
        self.assert_code(
            base_request(measurements=[item(0, float("nan")), item(1, 1.0)]), "invalid_throughput"
        )
        self.assert_code(
            base_request(
                measurements=[
                    {"timestamp_s": 0, "measured_capacity_ah": 9.0},
                    item(1, 1.0),
                ]
            ),
            "invalid_throughput",
        )

    def test_end_of_life_soh_rules(self) -> None:
        self.assert_code(base_request(end_of_life_soh=-0.1), "invalid_end_of_life_soh")
        self.assert_code(base_request(end_of_life_soh=1.1), "invalid_end_of_life_soh")
        self.assert_code(base_request(end_of_life_soh="x"), "invalid_end_of_life_soh")
        self.assert_code(base_request(end_of_life_soh=float("nan")), "invalid_end_of_life_soh")


if __name__ == "__main__":
    unittest.main()
