import unittest

from powerrail.service import ApiError, Service


def base_request(**overrides):
    payload = {
        "capacity_ah": 1.0,
        "initial_soc": 0.8,
        "samples": [
            {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 3.8},
            {"timestamp_s": 1800, "current_a": 1.0, "voltage_v": 3.7},
        ],
    }
    payload.update(overrides)
    return payload


class CoulombCountingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def test_first_sample_uses_initial_soc(self) -> None:
        result = self.service.estimate_soc(base_request())
        first = result["estimates"][0]
        self.assertEqual(first["timestamp_s"], 0)
        self.assertEqual(first["soc"], 0.8)
        self.assertEqual(first["source"], "coulomb")

    def test_discharge_with_average_current(self) -> None:
        payload = {
            "capacity_ah": 1.0,
            "initial_soc": 0.8,
            "samples": [
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.8},
                {"timestamp_s": 1800, "current_a": 2.0, "voltage_v": 3.7},
            ],
        }
        result = self.service.estimate_soc(payload)
        # average current 1.0 A over 1800 s = 0.5 Ah against 1 Ah capacity
        self.assertAlmostEqual(result["estimates"][1]["soc"], 0.3)
        self.assertEqual(result["estimates"][1]["source"], "coulomb")
        self.assertEqual(result["final_soc"], result["estimates"][-1]["soc"])

    def test_charge_moves_soc_up_and_clamps_to_unit_range(self) -> None:
        payload = {
            "capacity_ah": 1.0,
            "initial_soc": 0.95,
            "samples": [
                {"timestamp_s": 0, "current_a": -2.0, "voltage_v": 4.1},
                {"timestamp_s": 3600, "current_a": -2.0, "voltage_v": 4.2},
            ],
        }
        result = self.service.estimate_soc(payload)
        self.assertEqual(result["estimates"][-1]["soc"], 1.0)

    def test_estimates_match_sample_count_and_order(self) -> None:
        payload = {
            "capacity_ah": 2.0,
            "initial_soc": 0.5,
            "samples": [
                {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 4.0},
                {"timestamp_s": 600, "current_a": 1.0, "voltage_v": 3.9},
                {"timestamp_s": 1200, "current_a": 1.0, "voltage_v": 3.8},
                {"timestamp_s": 1800, "current_a": 1.0, "voltage_v": 3.7},
            ],
        }
        result = self.service.estimate_soc(payload)
        self.assertEqual(len(result["estimates"]), 4)
        self.assertEqual(
            [item["timestamp_s"] for item in result["estimates"]], [0, 600, 1200, 1800]
        )
        self.assertEqual(result["final_soc"], result["estimates"][-1]["soc"])


class OcvCorrectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.curve = [
            {"voltage_v": 3.0, "soc": 0.0},
            {"voltage_v": 4.0, "soc": 1.0},
        ]

    def _rest_payload(self, **overrides):
        payload = {
            "capacity_ah": 1.0,
            "initial_soc": 0.5,
            "ocv_curve": self.curve,
            "samples": [
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.8},
                {"timestamp_s": 150, "current_a": 0.0, "voltage_v": 3.8},
                {"timestamp_s": 300, "current_a": 0.0, "voltage_v": 3.8},
            ],
        }
        payload.update(overrides)
        return payload

    def test_no_correction_before_rest_duration(self) -> None:
        payload = self._rest_payload(
            samples=[
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.8},
                {"timestamp_s": 299, "current_a": 0.0, "voltage_v": 3.8},
            ]
        )
        result = self.service.estimate_soc(payload)
        self.assertEqual(result["estimates"][-1]["source"], "coulomb")

    def test_correction_once_rest_duration_reached(self) -> None:
        result = self.service.estimate_soc(self._rest_payload())
        corrected = result["estimates"][-1]
        self.assertEqual(corrected["source"], "ocv_corrected")
        # soc_next stays 0.5 at zero current; OCV interpolation yields 0.8
        self.assertAlmostEqual(corrected["soc"], 0.8 * 0.2 + 0.5 * 0.8)

    def test_current_above_threshold_resets_rest_timer(self) -> None:
        payload = self._rest_payload(
            samples=[
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.8},
                {"timestamp_s": 200, "current_a": 0.0, "voltage_v": 3.8},
                {"timestamp_s": 400, "current_a": 1.0, "voltage_v": 3.8},
                {"timestamp_s": 600, "current_a": 0.0, "voltage_v": 3.8},
                {"timestamp_s": 800, "current_a": 0.0, "voltage_v": 3.8},
            ]
        )
        result = self.service.estimate_soc(payload)
        self.assertEqual(result["estimates"][-1]["source"], "coulomb")

    def test_interval_boundaries_must_both_be_below_threshold(self) -> None:
        payload = self._rest_payload(
            rest_current_a=0.05,
            samples=[
                {"timestamp_s": 0, "current_a": 0.1, "voltage_v": 3.8},
                {"timestamp_s": 300, "current_a": 0.0, "voltage_v": 3.8},
            ],
        )
        result = self.service.estimate_soc(payload)
        self.assertEqual(result["estimates"][-1]["source"], "coulomb")

    def test_out_of_range_voltage_uses_nearest_endpoint(self) -> None:
        payload = self._rest_payload(
            samples=[
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 2.5},
                {"timestamp_s": 150, "current_a": 0.0, "voltage_v": 2.5},
                {"timestamp_s": 300, "current_a": 0.0, "voltage_v": 2.5},
            ]
        )
        result = self.service.estimate_soc(payload)
        corrected = result["estimates"][-1]
        self.assertEqual(corrected["source"], "ocv_corrected")
        self.assertAlmostEqual(corrected["soc"], 0.5 * 0.8)


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def assert_code(self, payload, code, status=422):
        with self.assertRaises(ApiError) as ctx:
            self.service.estimate_soc(payload)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.status, status)

    def test_non_object_payload_is_invalid_json(self) -> None:
        for payload in (None, [], "text", 5):
            self.assert_code(payload, "invalid_json", status=400)

    def test_capacity_rules(self) -> None:
        self.assert_code(base_request(capacity_ah=None), "invalid_capacity")
        self.assert_code(base_request(capacity_ah=0), "invalid_capacity")
        self.assert_code(base_request(capacity_ah=-1), "invalid_capacity")
        self.assert_code(base_request(capacity_ah=float("nan")), "invalid_capacity")
        self.assert_code(
            {k: v for k, v in base_request().items() if k != "capacity_ah"},
            "invalid_capacity",
        )

    def test_initial_soc_rules(self) -> None:
        self.assert_code(base_request(initial_soc=None), "invalid_initial_soc")
        self.assert_code(base_request(initial_soc=-0.1), "invalid_initial_soc")
        self.assert_code(base_request(initial_soc=1.1), "invalid_initial_soc")
        self.assert_code(base_request(initial_soc=float("inf")), "invalid_initial_soc")

    def test_samples_rules(self) -> None:
        self.assert_code(base_request(samples=[]), "invalid_samples")
        self.assert_code(base_request(samples="x"), "invalid_samples")
        self.assert_code(base_request(samples=None), "invalid_samples")

    def test_timestamp_rules(self) -> None:
        bad_sample = [
            {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 3.8},
            {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 3.8},
        ]
        self.assert_code(base_request(samples=bad_sample), "invalid_timestamp")
        missing = [{"current_a": 1.0, "voltage_v": 3.8}]
        self.assert_code(base_request(samples=missing), "invalid_timestamp")

    def test_measurement_rules(self) -> None:
        missing_current = [
            {"timestamp_s": 0, "voltage_v": 3.8},
            {"timestamp_s": 1, "voltage_v": 3.8},
        ]
        self.assert_code(base_request(samples=missing_current), "invalid_measurement")
        non_finite = [
            {"timestamp_s": 0, "current_a": 1.0, "voltage_v": 3.8},
            {"timestamp_s": 1, "current_a": float("nan"), "voltage_v": 3.8},
        ]
        self.assert_code(base_request(samples=non_finite), "invalid_measurement")

    def test_ocv_curve_rules(self) -> None:
        self.assert_code(
            base_request(ocv_curve=[{"voltage_v": 3.0, "soc": 0.0}]),
            "invalid_ocv_curve",
        )
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"voltage_v": 4.0, "soc": 0.0},
                    {"voltage_v": 3.0, "soc": 1.0},
                ]
            ),
            "invalid_ocv_curve",
        )
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"voltage_v": 3.0, "soc": 0.8},
                    {"voltage_v": 4.0, "soc": 0.2},
                ]
            ),
            "invalid_ocv_curve",
        )
        self.assert_code(
            base_request(
                ocv_curve=[
                    {"voltage_v": 3.0, "soc": 1.2},
                    {"voltage_v": 4.0, "soc": 1.0},
                ]
            ),
            "invalid_ocv_curve",
        )

    def test_option_rules(self) -> None:
        self.assert_code(
            base_request(rest_current_a=-0.01), "invalid_options"
        )
        self.assert_code(
            base_request(rest_duration_s=-1), "invalid_options"
        )
        self.assert_code(base_request(ocv_weight=1.1), "invalid_options")
        self.assert_code(base_request(ocv_weight="x"), "invalid_options")

    def test_custom_options_are_honored(self) -> None:
        curve = [
            {"voltage_v": 3.0, "soc": 0.0},
            {"voltage_v": 4.0, "soc": 1.0},
        ]
        payload = {
            "capacity_ah": 1.0,
            "initial_soc": 0.5,
            "ocv_curve": curve,
            "rest_current_a": 0.5,
            "rest_duration_s": 100,
            "ocv_weight": 0.5,
            "samples": [
                {"timestamp_s": 0, "current_a": 0.0, "voltage_v": 3.8},
                {"timestamp_s": 100, "current_a": 0.0, "voltage_v": 3.8},
            ],
        }
        result = self.service.estimate_soc(payload)
        item = result["estimates"][-1]
        self.assertEqual(item["source"], "ocv_corrected")
        self.assertAlmostEqual(item["soc"], 0.65)


if __name__ == "__main__":
    unittest.main()
