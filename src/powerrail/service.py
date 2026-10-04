"""Core service surface for PowerRail.

The baseline reports process health; battery state-of-charge estimation is
served from :meth:`Service.estimate_soc`. Keep the public surface here
backward compatible and express all client failures as :class:`ApiError`.
"""

from __future__ import annotations

import math
from typing import Any

from . import __version__

DEFAULT_REST_CURRENT_A = 0.05
DEFAULT_REST_DURATION_S = 300.0
DEFAULT_OCV_WEIGHT = 0.2
DEFAULT_END_OF_LIFE_SOH = 0.8
DEFAULT_MEDIAN_WINDOW = 3
DEFAULT_SMOOTHING_FACTOR = 0.25
DEFAULT_RESET_GAP_S = 30.0
DEFAULT_REGRESSION_THRESHOLD_PERCENT = 5.0
DEFAULT_TREND_THRESHOLD_W_PER_HOUR = 0.0
DEFAULT_MIN_VOLTAGE_STEP_V = 0.000001
DEFAULT_MIN_CURRENT_STEP_A = 0.1


class ApiError(Exception):
    """Client error carrying an HTTP status and a stable machine-readable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _is_finite_number(value: Any) -> bool:
    # ``bool`` is a subclass of ``int`` but is not a valid numeric reading.
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _median(values: list[float]) -> float:
    """Median of a non-empty list; even counts average the middle pair."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _interpolate_ocv(voltage: float, points: list[tuple[float, float]]) -> float:
    """Linearly interpolate SoC from a voltage-sorted OCV curve.

    Voltages outside the curve resolve to the nearest endpoint.
    """
    first_voltage, first_soc = points[0]
    last_voltage, last_soc = points[-1]
    if voltage <= first_voltage:
        return first_soc
    if voltage >= last_voltage:
        return last_soc
    for (v0, s0), (v1, s1) in zip(points, points[1:]):
        if voltage <= v1:
            ratio = (voltage - v0) / (v1 - v0)
            return s0 + ratio * (s1 - s0)
    return last_soc  # pragma: no cover - unreachable for a sorted curve


def _interpolate_model_ocv(
    soc: float, points: list[tuple[float, float]]
) -> float:
    """Linearly interpolate OCV voltage from a SoC-sorted curve.

    SoC values outside the curve resolve to the nearest endpoint.
    """
    first_soc, first_voltage = points[0]
    last_soc, last_voltage = points[-1]
    if soc <= first_soc:
        return first_voltage
    if soc >= last_soc:
        return last_voltage
    for (s0, v0), (s1, v1) in zip(points, points[1:]):
        if soc <= s1:
            ratio = (soc - s0) / (s1 - s0)
            return v0 + ratio * (v1 - v0)
    return last_voltage  # pragma: no cover - unreachable for a sorted curve


def _interpolate_efficiency(
    current: float, points: list[tuple[float, float]]
) -> float:
    """Linearly interpolate efficiency from a current-sorted curve.

    Currents outside the curve resolve to the nearest endpoint.
    """
    first_current, first_efficiency = points[0]
    last_current, last_efficiency = points[-1]
    if current <= first_current:
        return first_efficiency
    if current >= last_current:
        return last_efficiency
    for (c0, e0), (c1, e1) in zip(points, points[1:]):
        if current <= c1:
            ratio = (current - c0) / (c1 - c0)
            return e0 + ratio * (e1 - e0)
    return last_efficiency  # pragma: no cover - unreachable for a sorted curve


def _interpolate_dropout(
    current: float, points: list[tuple[float, float]]
) -> float:
    """Linearly interpolate dropout voltage from a current-sorted curve.

    Currents outside the curve resolve to the nearest endpoint.
    """
    first_current, first_dropout = points[0]
    last_current, last_dropout = points[-1]
    if current <= first_current:
        return first_dropout
    if current >= last_current:
        return last_dropout
    for (c0, d0), (c1, d1) in zip(points, points[1:]):
        if current <= c1:
            ratio = (current - c0) / (c1 - c0)
            return d0 + ratio * (d1 - d0)
    return last_dropout  # pragma: no cover - unreachable for a sorted curve


def _interpolate_cycle_life(
    depth: float, points: list[tuple[float, float]]
) -> float:
    """Linearly interpolate cycles-to-EOL from a depth-sorted life curve.

    Depths outside the curve resolve to the nearest endpoint.
    """
    first_depth, first_cycles = points[0]
    last_depth, last_cycles = points[-1]
    if depth <= first_depth:
        return first_cycles
    if depth >= last_depth:
        return last_cycles
    for (d0, c0), (d1, c1) in zip(points, points[1:]):
        if depth <= d1:
            ratio = (depth - d0) / (d1 - d0)
            return c0 + ratio * (c1 - c0)
    return last_cycles  # pragma: no cover - unreachable for a sorted curve


def _turning_points(socs: list[float]) -> list[float]:
    """Reduce a SoC series to endpoints and strict reversal points.

    Adjacent equal values collapse first; afterwards every interior
    point whose neighbours move in strictly opposite directions is a
    reversal and is kept alongside the first and last values.
    """
    compressed: list[float] = []
    for soc in socs:
        if not compressed or soc != compressed[-1]:
            compressed.append(soc)
    if len(compressed) <= 2:
        return compressed
    points = [compressed[0]]
    for index in range(1, len(compressed) - 1):
        rise_in = compressed[index] - compressed[index - 1]
        rise_out = compressed[index + 1] - compressed[index]
        if rise_in * rise_out < 0.0:
            points.append(compressed[index])
    points.append(compressed[-1])
    return points


def _rainflow_cycles(points: list[float]) -> list[tuple[float, float]]:
    """Count cycles in a turning-point series per ASTM E1049.

    Closed cycles count 1; each range left in the final residue counts
    0.5. Returns ``(depth, count)`` pairs in identification order.
    """
    stack: list[float] = []
    cycles: list[tuple[float, float]] = []
    for point in points:
        stack.append(point)
        while len(stack) >= 3:
            inner = abs(stack[-2] - stack[-3])
            outer = abs(stack[-1] - stack[-2])
            if outer < inner:
                break
            if len(stack) == 3:
                cycles.append((inner, 0.5))
                stack.pop(0)
            else:
                cycles.append((inner, 1.0))
                last = stack.pop()
                stack.pop()
                stack.pop()
                stack.append(last)
    for first, second in zip(stack, stack[1:]):
        cycles.append((abs(second - first), 0.5))
    return cycles


class Service:
    """Stateless service surface: health reporting and SoC estimation."""

    name = "powerrail"
    version = __version__

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    def estimate_soc(self, payload: Any) -> dict[str, Any]:
        """Validate and run a coulomb-counting SoC estimate with OCV correction."""
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        capacity = payload.get("capacity_ah")
        if not _is_finite_number(capacity) or capacity <= 0:
            raise ApiError(
                422, "invalid_capacity", "capacity_ah must be a positive finite number"
            )

        initial_soc = payload.get("initial_soc")
        if not _is_finite_number(initial_soc) or not 0 <= initial_soc <= 1:
            raise ApiError(
                422,
                "invalid_initial_soc",
                "initial_soc must be a finite number within [0, 1]",
            )

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "invalid_samples", "samples must be a non-empty array")

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(
                    422,
                    "invalid_timestamp",
                    "each sample must be an object with a finite timestamp_s",
                )
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            timestamps.append(timestamp)
            previous_timestamp = timestamp

        currents: list[float] = []
        voltages: list[float] = []
        for sample in samples:
            current = sample.get("current_a")
            voltage = sample.get("voltage_v")
            if not _is_finite_number(current) or not _is_finite_number(voltage):
                raise ApiError(
                    422,
                    "invalid_measurement",
                    "current_a and voltage_v must be finite numbers",
                )
            currents.append(current)
            voltages.append(voltage)

        ocv_points = self._parse_ocv_curve(payload.get("ocv_curve"))
        rest_current, rest_duration, ocv_weight = self._parse_options(payload)

        estimates: list[dict[str, Any]] = []
        soc = initial_soc
        rest_elapsed = 0.0
        capacity_seconds = capacity * 3600.0

        for index in range(len(samples)):
            timestamp = timestamps[index]
            if index == 0:
                estimates.append(
                    {"timestamp_s": timestamp, "soc": soc, "source": "coulomb"}
                )
                continue

            delta_time = timestamp - timestamps[index - 1]
            average_current = (currents[index - 1] + currents[index]) / 2.0
            soc_next = soc - average_current * delta_time / capacity_seconds
            if soc_next < 0.0:
                soc_next = 0.0
            elif soc_next > 1.0:
                soc_next = 1.0

            source = "coulomb"
            if ocv_points is not None:
                if (
                    abs(currents[index - 1]) <= rest_current
                    and abs(currents[index]) <= rest_current
                ):
                    rest_elapsed += delta_time
                else:
                    rest_elapsed = 0.0
                if rest_elapsed >= rest_duration:
                    ocv_soc = _interpolate_ocv(voltages[index], ocv_points)
                    soc_next = (
                        1.0 - ocv_weight
                    ) * soc_next + ocv_weight * ocv_soc
                    source = "ocv_corrected"

            soc = soc_next
            estimates.append(
                {"timestamp_s": timestamp, "soc": soc, "source": source}
            )

        return {"estimates": estimates, "final_soc": soc}

    def simulate_battery_model(self, payload: Any) -> dict[str, Any]:
        """Simulate terminal voltage with a first-order Thevenin model.

        The first estimate uses ``initial_soc`` with zero polarization.
        Each later interval integrates the *previous* sample's current:
        SoC drops by coulomb counting (clamped to [0, 1]) and the RC
        polarization voltage follows its zero-order-hold response. OCV is
        linearly interpolated from the SoC-keyed curve, and the terminal
        voltage subtracts the ohmic drop of the current sample's current
        and the polarization voltage.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        capacity = payload.get("capacity_ah")
        if not _is_finite_number(capacity) or capacity <= 0:
            raise ApiError(
                422, "invalid_capacity", "capacity_ah must be a positive finite number"
            )

        initial_soc = payload.get("initial_soc")
        if not _is_finite_number(initial_soc) or not 0 <= initial_soc <= 1:
            raise ApiError(
                422,
                "invalid_initial_soc",
                "initial_soc must be a finite number within [0, 1]",
            )

        r0, r1, c1 = self._parse_battery_model(payload.get("model"))
        ocv_points = self._parse_model_ocv_curve(payload.get("ocv_curve"))

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "invalid_samples", "samples must be a non-empty array")
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(422, "invalid_samples", "each sample must be an object")

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            timestamps.append(float(timestamp))
            previous_timestamp = timestamp

        currents: list[float] = []
        for sample in samples:
            current = sample.get("current_a")
            if not _is_finite_number(current):
                raise ApiError(
                    422, "invalid_current", "current_a must be a finite number"
                )
            currents.append(float(current))

        capacity = float(capacity)
        capacity_seconds = capacity * 3600.0
        time_constant = r1 * c1

        estimates: list[dict[str, Any]] = []
        soc = float(initial_soc)
        polarization = 0.0
        for index in range(len(samples)):
            if index > 0:
                delta_time = timestamps[index] - timestamps[index - 1]
                previous_current = currents[index - 1]
                soc -= previous_current * delta_time / capacity_seconds
                if soc < 0.0:
                    soc = 0.0
                elif soc > 1.0:
                    soc = 1.0
                decay = math.exp(-delta_time / time_constant)
                polarization = (
                    polarization * decay + r1 * previous_current * (1.0 - decay)
                )
            ocv = _interpolate_model_ocv(soc, ocv_points)
            terminal = ocv - currents[index] * r0 - polarization
            estimates.append(
                {
                    "timestamp_s": samples[index]["timestamp_s"],
                    "soc": soc,
                    "ocv_voltage_v": ocv,
                    "polarization_voltage_v": polarization,
                    "terminal_voltage_v": terminal,
                }
            )

        return {
            "estimates": estimates,
            "final_soc": estimates[-1]["soc"],
            "final_terminal_voltage_v": estimates[-1]["terminal_voltage_v"],
        }

    def estimate_internal_resistance(self, payload: Any) -> dict[str, Any]:
        """Fit a SoC-keyed ohmic internal-resistance curve from current pulses.

        Each pulse reports the terminal voltage response to a current
        step; its single-pulse resistance is ``-delta_voltage_v /
        delta_current_a``. Pulses sharing a SoC merge into one knot via a
        weight-weighted mean, and every pulse is scored against the
        resistance of the knot it belongs to.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        pulses = payload.get("pulses")
        if not isinstance(pulses, list) or not pulses:
            raise ApiError(422, "invalid_pulses", "pulses must be a non-empty array")
        for pulse in pulses:
            if not isinstance(pulse, dict):
                raise ApiError(422, "invalid_pulses", "each pulse must be an object")

        socs: list[float] = []
        for pulse in pulses:
            soc = pulse.get("soc")
            if not _is_finite_number(soc) or not 0 <= soc <= 1:
                raise ApiError(
                    422, "invalid_soc", "soc must be a finite number within [0, 1]"
                )
            socs.append(float(soc))

        current_before: list[float] = []
        current_after: list[float] = []
        for pulse in pulses:
            before = pulse.get("current_before_a")
            after = pulse.get("current_after_a")
            if not _is_finite_number(before) or not _is_finite_number(after):
                raise ApiError(
                    422,
                    "invalid_current",
                    "current_before_a and current_after_a must be finite numbers",
                )
            current_before.append(float(before))
            current_after.append(float(after))

        voltage_before: list[float] = []
        voltage_after: list[float] = []
        for pulse in pulses:
            before = pulse.get("voltage_before_v")
            after = pulse.get("voltage_after_v")
            if (
                not _is_finite_number(before)
                or before <= 0
                or not _is_finite_number(after)
                or after <= 0
            ):
                raise ApiError(
                    422,
                    "invalid_voltage",
                    "voltage_before_v and voltage_after_v must be positive "
                    "finite numbers",
                )
            voltage_before.append(float(before))
            voltage_after.append(float(after))

        weights: list[float] = []
        for pulse in pulses:
            weight = pulse.get("weight", 1.0)
            if not _is_finite_number(weight) or weight <= 0:
                raise ApiError(
                    422, "invalid_weight", "weight must be a positive finite number"
                )
            weights.append(float(weight))

        min_current_step = payload.get(
            "min_current_step_a", DEFAULT_MIN_CURRENT_STEP_A
        )
        if not _is_finite_number(min_current_step) or min_current_step <= 0:
            raise ApiError(
                422,
                "invalid_options",
                "min_current_step_a must be a positive finite number",
            )
        min_current_step = float(min_current_step)

        delta_currents = [
            current_after[index] - current_before[index]
            for index in range(len(pulses))
        ]
        delta_voltages = [
            voltage_after[index] - voltage_before[index]
            for index in range(len(pulses))
        ]
        for index in range(len(pulses)):
            if abs(delta_currents[index]) < min_current_step:
                raise ApiError(
                    422,
                    "invalid_current_step",
                    "current step magnitude must reach min_current_step_a",
                )
            if delta_currents[index] * delta_voltages[index] >= 0.0:
                raise ApiError(
                    422,
                    "invalid_pulse_response",
                    "voltage change must oppose current change",
                )

        knot_weight_sums: dict[float, float] = {}
        knot_resistance_sums: dict[float, float] = {}
        knot_pulse_counts: dict[float, int] = {}
        resistances: list[float] = []
        for index, soc in enumerate(socs):
            resistance = -delta_voltages[index] / delta_currents[index]
            resistances.append(resistance)
            knot_weight_sums[soc] = knot_weight_sums.get(soc, 0.0) + weights[index]
            knot_resistance_sums[soc] = (
                knot_resistance_sums.get(soc, 0.0) + weights[index] * resistance
            )
            knot_pulse_counts[soc] = knot_pulse_counts.get(soc, 0) + 1

        unique_socs = sorted(knot_weight_sums)
        if len(unique_socs) < 2:
            raise ApiError(
                422,
                "insufficient_soc_span",
                "pulses must cover at least two distinct soc values",
            )

        knot_resistances = {
            soc: knot_resistance_sums[soc] / knot_weight_sums[soc]
            for soc in unique_socs
        }

        pulse_estimates: list[dict[str, Any]] = []
        weighted_error_sum = 0.0
        weight_total = 0.0
        for index in range(len(pulses)):
            fitted_voltage_change = (
                -delta_currents[index] * knot_resistances[socs[index]]
            )
            residual = delta_voltages[index] - fitted_voltage_change
            weighted_error_sum += weights[index] * residual * residual
            weight_total += weights[index]
            pulse_estimates.append(
                {
                    "index": index,
                    "soc": pulses[index]["soc"],
                    "delta_current_a": delta_currents[index],
                    "delta_voltage_v": delta_voltages[index],
                    "resistance_ohm": resistances[index],
                    "fitted_voltage_change_v": fitted_voltage_change,
                    "residual_voltage_v": residual,
                }
            )

        curve = [
            {
                "soc": soc,
                "resistance_ohm": knot_resistances[soc],
                "pulse_count": knot_pulse_counts[soc],
                "weight_sum": knot_weight_sums[soc],
            }
            for soc in unique_socs
        ]

        return {
            "status": "fitted",
            "curve": curve,
            "pulse_estimates": pulse_estimates,
            "recommended_r0_ohm": max(knot_resistances[soc] for soc in unique_socs),
            "rmse_voltage_v": math.sqrt(weighted_error_sum / weight_total),
        }

    def fit_ocv_curve(self, payload: Any) -> dict[str, Any]:
        """Fit a monotone OCV curve from rest calibration measurements.

        Measurements sharing a SoC merge into one knot; knot voltages
        minimize the weighted sum of squared voltage residuals subject
        to adjacent knots rising by at least ``min_voltage_step_v``.
        Substituting ``z_k = y_k - k * min_voltage_step_v`` turns the
        gap constraint into a plain monotonicity constraint, so the
        weighted pool-adjacent-violators algorithm on the shifted
        targets gives the exact least-squares solution.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        measurements = payload.get("measurements")
        if not isinstance(measurements, list) or len(measurements) < 2:
            raise ApiError(
                422,
                "invalid_measurements",
                "measurements must be an array with at least two items",
            )
        for measurement in measurements:
            if not isinstance(measurement, dict):
                raise ApiError(
                    422,
                    "invalid_measurements",
                    "each measurement must be an object",
                )

        socs: list[float] = []
        for measurement in measurements:
            soc = measurement.get("soc")
            if not _is_finite_number(soc) or not 0 <= soc <= 1:
                raise ApiError(
                    422, "invalid_soc", "soc must be a finite number within [0, 1]"
                )
            socs.append(float(soc))

        voltages: list[float] = []
        for measurement in measurements:
            voltage = measurement.get("voltage_v")
            if not _is_finite_number(voltage):
                raise ApiError(
                    422, "invalid_voltage", "voltage_v must be a finite number"
                )
            voltages.append(float(voltage))

        weights: list[float] = []
        for measurement in measurements:
            weight = measurement.get("weight", 1.0)
            if not _is_finite_number(weight) or weight <= 0:
                raise ApiError(
                    422, "invalid_weight", "weight must be a positive finite number"
                )
            weights.append(float(weight))

        min_voltage_step = self._parse_fit_options(payload.get("options"))

        knot_weight_sums: dict[float, float] = {}
        knot_voltage_sums: dict[float, float] = {}
        knot_sample_counts: dict[float, int] = {}
        for soc, voltage, weight in zip(socs, voltages, weights):
            knot_weight_sums[soc] = knot_weight_sums.get(soc, 0.0) + weight
            knot_voltage_sums[soc] = (
                knot_voltage_sums.get(soc, 0.0) + weight * voltage
            )
            knot_sample_counts[soc] = knot_sample_counts.get(soc, 0) + 1

        unique_socs = sorted(knot_weight_sums)
        knot_total = len(unique_socs)
        if knot_total < 2:
            raise ApiError(
                422,
                "insufficient_soc_span",
                "measurements must cover at least two distinct soc values",
            )

        weight_sums = [knot_weight_sums[soc] for soc in unique_socs]
        targets = [
            knot_voltage_sums[soc] / knot_weight_sums[soc]
            - index * min_voltage_step
            for index, soc in enumerate(unique_socs)
        ]

        # Weighted PAVA on the shifted targets; each block is
        # (weight_sum, weighted_target_sum, first knot index).
        blocks: list[tuple[float, float, int]] = []
        for index in range(knot_total):
            blocks.append(
                (weight_sums[index], weight_sums[index] * targets[index], index)
            )
            while len(blocks) >= 2:
                prev_weight, prev_sum, prev_start = blocks[-2]
                cur_weight, cur_sum, _ = blocks[-1]
                if prev_sum / prev_weight <= cur_sum / cur_weight:
                    break
                blocks[-2:] = [
                    (prev_weight + cur_weight, prev_sum + cur_sum, prev_start)
                ]

        knot_voltages = [0.0] * knot_total
        block_starts = [start for _, _, start in blocks] + [knot_total]
        for block_index, (block_weight, block_sum, _) in enumerate(blocks):
            shifted = block_sum / block_weight
            for index in range(block_starts[block_index], block_starts[block_index + 1]):
                knot_voltages[index] = shifted + index * min_voltage_step

        fitted_by_soc = {
            soc: knot_voltages[index] for index, soc in enumerate(unique_socs)
        }

        residuals: list[dict[str, Any]] = []
        weighted_error_sum = 0.0
        weight_total = 0.0
        max_abs_error = 0.0
        for index in range(len(measurements)):
            fitted = fitted_by_soc[socs[index]]
            residual = voltages[index] - fitted
            weighted_error_sum += weights[index] * residual * residual
            weight_total += weights[index]
            if abs(residual) > max_abs_error:
                max_abs_error = abs(residual)
            residuals.append(
                {
                    "index": index,
                    "soc": measurements[index]["soc"],
                    "measured_voltage_v": measurements[index]["voltage_v"],
                    "fitted_voltage_v": fitted,
                    "residual_voltage_v": residual,
                }
            )

        curve = [
            {
                "soc": unique_socs[index],
                "voltage_v": knot_voltages[index],
                "sample_count": knot_sample_counts[unique_socs[index]],
                "weight_sum": weight_sums[index],
            }
            for index in range(knot_total)
        ]

        return {
            "status": "fitted",
            "curve": curve,
            "residuals": residuals,
            "rmse_voltage_v": math.sqrt(weighted_error_sum / weight_total),
            "max_abs_error_v": max_abs_error,
            "measurement_count": len(measurements),
            "knot_count": knot_total,
        }

    def estimate_health(self, payload: Any) -> dict[str, Any]:
        """Estimate battery state of health and remaining cycle life."""
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        nominal_capacity = payload.get("nominal_capacity_ah")
        if not _is_finite_number(nominal_capacity) or nominal_capacity <= 0:
            raise ApiError(
                422,
                "invalid_nominal_capacity",
                "nominal_capacity_ah must be a positive finite number",
            )

        measurements = payload.get("measurements")
        if not isinstance(measurements, list) or len(measurements) < 2:
            raise ApiError(
                422,
                "invalid_measurements",
                "measurements must be an array with at least two items",
            )
        for measurement in measurements:
            if not isinstance(measurement, dict):
                raise ApiError(
                    422,
                    "invalid_measurements",
                    "each measurement must be an object",
                )

        end_of_life_soh = payload.get("end_of_life_soh", DEFAULT_END_OF_LIFE_SOH)
        if (
            not _is_finite_number(end_of_life_soh)
            or not 0 <= end_of_life_soh <= 1
        ):
            raise ApiError(
                422,
                "invalid_end_of_life_soh",
                "end_of_life_soh must be a finite number within [0, 1]",
            )

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for measurement in measurements:
            timestamp = measurement.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            timestamps.append(timestamp)
            previous_timestamp = timestamp

        measured_capacities: list[float] = []
        for measurement in measurements:
            measured_capacity = measurement.get("measured_capacity_ah")
            if not _is_finite_number(measured_capacity) or measured_capacity <= 0:
                raise ApiError(
                    422,
                    "invalid_capacity_measurement",
                    "measured_capacity_ah must be a positive finite number",
                )
            measured_capacities.append(measured_capacity)

        cumulative_discharge: list[float] = []
        previous_throughput: float | None = None
        for measurement in measurements:
            throughput = measurement.get("cumulative_discharge_ah")
            if not _is_finite_number(throughput) or throughput < 0:
                raise ApiError(
                    422,
                    "invalid_throughput",
                    "cumulative_discharge_ah must be a non-negative finite number",
                )
            if previous_throughput is not None and throughput < previous_throughput:
                raise ApiError(
                    422,
                    "invalid_throughput",
                    "cumulative_discharge_ah must be non-decreasing",
                )
            cumulative_discharge.append(throughput)
            previous_throughput = throughput

        estimates: list[dict[str, Any]] = []
        for index in range(len(measurements)):
            soh = measured_capacities[index] / nominal_capacity
            if soh < 0.0:
                soh = 0.0
            elif soh > 1.0:
                soh = 1.0
            equivalent_cycles = cumulative_discharge[index] / nominal_capacity
            estimates.append(
                {
                    "timestamp_s": timestamps[index],
                    "soh": soh,
                    "equivalent_cycles": equivalent_cycles,
                }
            )

        latest_soh = estimates[-1]["soh"]
        consumed_cycles = estimates[-1]["equivalent_cycles"]

        remaining_cycles: float | None
        if latest_soh <= end_of_life_soh:
            remaining_cycles = 0
            projection_status = "end_of_life"
        else:
            first_soh = estimates[0]["soh"]
            first_cycles = estimates[0]["equivalent_cycles"]
            cycle_span = consumed_cycles - first_cycles
            soh_drop = first_soh - latest_soh
            if cycle_span > 0 and soh_drop > 0:
                degradation_rate = soh_drop / cycle_span
                remaining_cycles = (latest_soh - end_of_life_soh) / degradation_rate
                projection_status = "projected"
            else:
                remaining_cycles = None
                projection_status = "insufficient_trend"

        return {
            "estimates": estimates,
            "latest_soh": latest_soh,
            "consumed_cycles": consumed_cycles,
            "remaining_cycles": remaining_cycles,
            "projection_status": projection_status,
        }

    def analyze_battery_cycles(self, payload: Any) -> dict[str, Any]:
        """Rainflow-count partial SoC cycles and score cycle aging.

        Adjacent equal SoC values collapse, then only the endpoints and
        strict reversal points remain. ASTM E1049 rainflow counting
        closes full cycles (count 1) and leaves residual ranges (count
        0.5); each cycle's depth is its SoC range. Cycles strictly
        shallower than ``min_cycle_depth`` are ignored; the rest look up
        ``cycles_to_eol`` on the life curve (linear interpolation,
        clamped to the nearest endpoint) and contribute
        ``count / cycles_to_eol`` damage.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        samples = payload.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            raise ApiError(
                422,
                "invalid_samples",
                "samples must be an array with at least two items",
            )
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(
                    422, "invalid_samples", "each sample must be an object"
                )

        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            previous_timestamp = timestamp

        socs: list[float] = []
        for sample in samples:
            soc = sample.get("soc")
            if not _is_finite_number(soc) or not 0 <= soc <= 1:
                raise ApiError(
                    422, "invalid_soc", "soc must be a finite number within [0, 1]"
                )
            socs.append(float(soc))

        life_curve = self._parse_cycle_life_curve(payload.get("cycle_life_curve"))

        min_cycle_depth = payload.get("min_cycle_depth", 0.0)
        if not _is_finite_number(min_cycle_depth) or not 0 <= min_cycle_depth <= 1:
            raise ApiError(
                422,
                "invalid_options",
                "min_cycle_depth must be a finite number within [0, 1]",
            )
        min_cycle_depth = float(min_cycle_depth)

        counted = _rainflow_cycles(_turning_points(socs))

        cycles: list[dict[str, Any]] = []
        cycle_count = 0.0
        equivalent_full_cycles = 0.0
        total_damage = 0.0
        for depth, count in counted:
            if depth < min_cycle_depth:
                continue
            cycles_to_eol = _interpolate_cycle_life(depth, life_curve)
            damage = count / cycles_to_eol
            cycles.append(
                {
                    "depth": depth,
                    "count": count,
                    "cycles_to_eol": cycles_to_eol,
                    "damage": damage,
                }
            )
            cycle_count += count
            equivalent_full_cycles += depth * count
            total_damage += damage

        return {
            "cycles": cycles,
            "cycle_count": cycle_count,
            "equivalent_full_cycles": equivalent_full_cycles,
            "total_damage": total_damage,
            "remaining_life_ratio": max(0.0, 1.0 - total_damage),
            "status": "exhausted" if total_damage >= 1.0 else "active",
        }

    def allocate_power_budget(self, payload: Any) -> dict[str, Any]:
        """Allocate a power budget across prioritized loads.

        Minimum power is funded first, highest priority level first; a
        level that cannot be fully funded splits the remainder
        proportionally to each load's minimum and lower levels get
        nothing. Leftover power then funds unmet demand the same way.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        available = payload.get("available_power_w")
        if not _is_finite_number(available) or available < 0:
            raise ApiError(
                422,
                "invalid_budget",
                "available_power_w must be a non-negative finite number",
            )

        reserve = payload.get("reserve_power_w", 0.0)
        if not _is_finite_number(reserve) or reserve < 0:
            raise ApiError(
                422,
                "invalid_budget",
                "reserve_power_w must be a non-negative finite number",
            )
        if reserve > available:
            raise ApiError(
                422,
                "invalid_budget",
                "reserve_power_w must not exceed available_power_w",
            )

        loads = payload.get("loads")
        if not isinstance(loads, list) or not loads:
            raise ApiError(422, "invalid_loads", "loads must be a non-empty array")
        for load in loads:
            if not isinstance(load, dict):
                raise ApiError(422, "invalid_loads", "each load must be an object")

        ids: list[str] = []
        demands: list[float] = []
        minimums: list[float] = []
        priorities: list[int] = []
        seen_ids: set[str] = set()
        for load in loads:
            load_id = load.get("id")
            if not isinstance(load_id, str) or not load_id:
                raise ApiError(
                    422, "invalid_load_id", "each load id must be a non-empty string"
                )
            if load_id in seen_ids:
                raise ApiError(
                    422, "invalid_load_id", f"load id {load_id!r} is duplicated"
                )
            seen_ids.add(load_id)
            ids.append(load_id)

            demand = load.get("demand_power_w")
            minimum = load.get("min_power_w")
            if not _is_finite_number(demand) or demand < 0:
                raise ApiError(
                    422,
                    "invalid_load_power",
                    "demand_power_w must be a non-negative finite number",
                )
            if not _is_finite_number(minimum) or minimum < 0:
                raise ApiError(
                    422,
                    "invalid_load_power",
                    "min_power_w must be a non-negative finite number",
                )
            if minimum > demand:
                raise ApiError(
                    422,
                    "invalid_load_power",
                    "min_power_w must not exceed demand_power_w",
                )
            demands.append(float(demand))
            minimums.append(float(minimum))

            priority = load.get("priority")
            if (
                not isinstance(priority, int)
                or isinstance(priority, bool)
                or not 0 <= priority <= 100
            ):
                raise ApiError(
                    422,
                    "invalid_priority",
                    "priority must be an integer within [0, 100]",
                )
            priorities.append(priority)

        distributable = available - reserve
        allocated = [0.0] * len(loads)
        remaining = self._fund_levels(minimums, priorities, ids, allocated, distributable)
        remaining = self._fund_levels(demands, priorities, ids, allocated, remaining)

        # Keep floating-point drift inside the documented invariants.
        for index in range(len(loads)):
            if allocated[index] < 0.0:
                allocated[index] = 0.0
            elif allocated[index] > demands[index]:
                allocated[index] = demands[index]
        excess = sum(allocated) - distributable
        if excess > 0.0:
            for index in sorted(range(len(loads)), key=lambda i: (-allocated[i], ids[i])):
                if excess <= 0.0:
                    break
                cut = min(allocated[index], excess)
                allocated[index] -= cut
                excess -= cut
        total_allocated = sum(allocated)
        unallocated = distributable - total_allocated
        if unallocated < 0.0:
            unallocated = 0.0

        allocations: list[dict[str, Any]] = []
        satisfied = True
        for index in range(len(loads)):
            shortfall = demands[index] - allocated[index]
            if shortfall < 0.0:
                shortfall = 0.0
            if demands[index] == 0.0 or allocated[index] >= demands[index]:
                state = "powered"
            elif allocated[index] > 0.0:
                state = "limited"
                satisfied = False
            else:
                state = "shed"
                satisfied = False
            allocations.append(
                {
                    "id": ids[index],
                    "allocated_power_w": allocated[index],
                    "shortfall_power_w": shortfall,
                    "state": state,
                }
            )

        return {
            "status": "satisfied" if satisfied else "constrained",
            "allocated_power_w": total_allocated,
            "unallocated_power_w": unallocated,
            "allocations": allocations,
        }

    LOAD_SHED_LEVELS = ("normal", "warning", "critical")

    def decide_load_shedding(self, payload: Any) -> dict[str, Any]:
        """Decide hysteresis load shedding across telemetry samples.

        Each sample's raw shortfall is total demand plus reserve minus
        available power, floored at zero. Reaching the critical or
        warning threshold targets the ``critical`` or ``warning`` level,
        anything lower targets ``normal``. The level starts at
        ``normal``; escalation is immediate (a two-level jump counts as
        one escalation), while de-escalation needs ``recovery_samples``
        consecutive samples whose target is below the current level to
        drop a single level, and any sample not below the current level
        resets that streak. ``warning`` sheds loads marked ``warning``,
        ``critical`` also sheds those marked ``critical``, and ``never``
        loads are never shed. Remaining loads follow the power budget
        rules; shed loads are allocated zero with state ``shed``.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        loads = payload.get("loads")
        if not isinstance(loads, list) or not loads:
            raise ApiError(422, "invalid_loads", "loads must be a non-empty array")
        for load in loads:
            if not isinstance(load, dict):
                raise ApiError(422, "invalid_loads", "each load must be an object")

        ids: list[str] = []
        demands: list[float] = []
        minimums: list[float] = []
        priorities: list[int] = []
        shed_levels: list[str] = []
        seen_ids: set[str] = set()
        for load in loads:
            load_id = load.get("id")
            if not isinstance(load_id, str) or not load_id:
                raise ApiError(
                    422, "invalid_load_id", "each load id must be a non-empty string"
                )
            if load_id in seen_ids:
                raise ApiError(
                    422, "invalid_load_id", f"load id {load_id!r} is duplicated"
                )
            seen_ids.add(load_id)
            ids.append(load_id)

            demand = load.get("demand_power_w")
            minimum = load.get("min_power_w")
            if not _is_finite_number(demand) or demand < 0:
                raise ApiError(
                    422,
                    "invalid_load_power",
                    "demand_power_w must be a non-negative finite number",
                )
            if not _is_finite_number(minimum) or minimum < 0:
                raise ApiError(
                    422,
                    "invalid_load_power",
                    "min_power_w must be a non-negative finite number",
                )
            if minimum > demand:
                raise ApiError(
                    422,
                    "invalid_load_power",
                    "min_power_w must not exceed demand_power_w",
                )
            demands.append(float(demand))
            minimums.append(float(minimum))

            priority = load.get("priority")
            if (
                not isinstance(priority, int)
                or isinstance(priority, bool)
                or not 0 <= priority <= 100
            ):
                raise ApiError(
                    422,
                    "invalid_priority",
                    "priority must be an integer within [0, 100]",
                )
            priorities.append(priority)

            shed_level = load.get("shed_level")
            if shed_level not in ("warning", "critical", "never"):
                raise ApiError(
                    422,
                    "invalid_shed_level",
                    "shed_level must be one of 'warning', 'critical' or 'never'",
                )
            shed_levels.append(shed_level)

        config = payload.get("config")
        if not isinstance(config, dict):
            raise ApiError(
                422, "invalid_load_shed_config", "config must be an object"
            )
        reserve = config.get("reserve_power_w")
        if not _is_finite_number(reserve) or reserve < 0:
            raise ApiError(
                422,
                "invalid_load_shed_config",
                "reserve_power_w must be a non-negative finite number",
            )
        warning_shortfall = config.get("warning_shortfall_w")
        critical_shortfall = config.get("critical_shortfall_w")
        if (
            not _is_finite_number(warning_shortfall)
            or not _is_finite_number(critical_shortfall)
            or not 0 < warning_shortfall < critical_shortfall
        ):
            raise ApiError(
                422,
                "invalid_load_shed_config",
                "thresholds must satisfy 0 < warning_shortfall_w < "
                "critical_shortfall_w",
            )
        recovery_samples = config.get("recovery_samples")
        if (
            not isinstance(recovery_samples, int)
            or isinstance(recovery_samples, bool)
            or recovery_samples < 1
        ):
            raise ApiError(
                422,
                "invalid_load_shed_config",
                "recovery_samples must be a positive integer",
            )

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "invalid_samples", "samples must be a non-empty array")
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(
                    422, "invalid_samples", "each sample must be an object"
                )

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            timestamps.append(timestamp)
            previous_timestamp = timestamp

        availables: list[float] = []
        for sample in samples:
            available = sample.get("available_power_w")
            if not _is_finite_number(available) or available < 0:
                raise ApiError(
                    422,
                    "invalid_available_power",
                    "available_power_w must be a non-negative finite number",
                )
            availables.append(float(available))

        reserve = float(reserve)
        total_demand = sum(demands)
        level_names = self.LOAD_SHED_LEVELS
        level = 0
        recovery_streak = 0
        escalation_count = 0

        decisions: list[dict[str, Any]] = []
        for index in range(len(samples)):
            raw_shortfall = total_demand + reserve - availables[index]
            if raw_shortfall < 0.0:
                raw_shortfall = 0.0
            if raw_shortfall >= critical_shortfall:
                target = 2
            elif raw_shortfall >= warning_shortfall:
                target = 1
            else:
                target = 0

            if target > level:
                level = target
                escalation_count += 1
                recovery_streak = 0
            elif target < level:
                recovery_streak += 1
                if recovery_streak >= recovery_samples:
                    level -= 1
                    recovery_streak = 0
            else:
                recovery_streak = 0

            shed_flags = [
                (level == 1 and shed_levels[position] == "warning")
                or (level == 2 and shed_levels[position] in ("warning", "critical"))
                for position in range(len(loads))
            ]

            distributable = availables[index] - reserve
            if distributable < 0.0:
                distributable = 0.0
            active = [
                position
                for position in range(len(loads))
                if not shed_flags[position]
            ]
            active_ids = [ids[position] for position in active]
            active_allocated = [0.0] * len(active)
            remaining = self._fund_levels(
                [minimums[position] for position in active],
                [priorities[position] for position in active],
                active_ids,
                active_allocated,
                distributable,
            )
            self._fund_levels(
                [demands[position] for position in active],
                [priorities[position] for position in active],
                active_ids,
                active_allocated,
                remaining,
            )

            allocated = [0.0] * len(loads)
            for position, share in zip(active, active_allocated):
                allocated[position] = share
            for position in active:
                if allocated[position] < 0.0:
                    allocated[position] = 0.0
                elif allocated[position] > demands[position]:
                    allocated[position] = demands[position]
            excess = sum(allocated) - distributable
            if excess > 0.0:
                for position in sorted(
                    active, key=lambda i: (-allocated[i], ids[i])
                ):
                    if excess <= 0.0:
                        break
                    cut = min(allocated[position], excess)
                    allocated[position] -= cut
                    excess -= cut
            total_allocated = sum(allocated)
            unallocated = distributable - total_allocated
            if unallocated < 0.0:
                unallocated = 0.0

            allocations: list[dict[str, Any]] = []
            satisfied = True
            for position in range(len(loads)):
                if shed_flags[position]:
                    allocations.append(
                        {
                            "id": ids[position],
                            "allocated_power_w": 0.0,
                            "shortfall_power_w": demands[position],
                            "state": "shed",
                        }
                    )
                    continue
                shortfall = demands[position] - allocated[position]
                if shortfall < 0.0:
                    shortfall = 0.0
                if demands[position] == 0.0 or allocated[position] >= demands[position]:
                    state = "powered"
                elif allocated[position] > 0.0:
                    state = "limited"
                    satisfied = False
                else:
                    state = "shed"
                    satisfied = False
                allocations.append(
                    {
                        "id": ids[position],
                        "allocated_power_w": allocated[position],
                        "shortfall_power_w": shortfall,
                        "state": state,
                    }
                )

            if all(shed_flags):
                status = "policy_shed"
            elif satisfied:
                status = "satisfied"
            else:
                status = "constrained"

            decisions.append(
                {
                    "timestamp_s": samples[index]["timestamp_s"],
                    "raw_shortfall_w": raw_shortfall,
                    "target_level": level_names[target],
                    "level": level_names[level],
                    "shed_ids": [
                        ids[position]
                        for position in range(len(loads))
                        if shed_flags[position]
                    ],
                    "status": status,
                    "allocated_power_w": total_allocated,
                    "unallocated_power_w": unallocated,
                    "allocations": allocations,
                }
            )

        return {
            "decisions": decisions,
            "final_level": level_names[level],
            "escalation_count": escalation_count,
        }

    def filter_telemetry(self, payload: Any) -> dict[str, Any]:
        """Denoise current/voltage telemetry with segment-aware median + EMA."""
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "invalid_samples", "samples must be a non-empty array")
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(422, "invalid_samples", "each sample must be an object")

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            timestamps.append(float(timestamp))
            previous_timestamp = timestamp

        currents: list[float] = []
        voltages: list[float] = []
        for sample in samples:
            current = sample.get("current_a")
            voltage = sample.get("voltage_v")
            if not _is_finite_number(current) or not _is_finite_number(voltage):
                raise ApiError(
                    422,
                    "invalid_measurement",
                    "current_a and voltage_v must be finite numbers",
                )
            currents.append(float(current))
            voltages.append(float(voltage))

        median_window, smoothing_factor, reset_gap_s = self._parse_filter_options(
            payload
        )

        filtered_samples: list[dict[str, Any]] = []
        current_history: list[float] = []
        voltage_history: list[float] = []
        previous_current: float | None = None
        previous_voltage: float | None = None
        segment_count = 0

        for index in range(len(samples)):
            segment_start = False
            if index == 0 or timestamps[index] - timestamps[index - 1] > reset_gap_s:
                segment_count += 1
                segment_start = True
                current_history = []
                voltage_history = []
                previous_current = None
                previous_voltage = None

            current_median = _median(current_history + [currents[index]])
            voltage_median = _median(voltage_history + [voltages[index]])
            if previous_current is None:
                filtered_current = current_median
                filtered_voltage = voltage_median
            else:
                filtered_current = (
                    smoothing_factor * current_median
                    + (1.0 - smoothing_factor) * previous_current
                )
                filtered_voltage = (
                    smoothing_factor * voltage_median
                    + (1.0 - smoothing_factor) * previous_voltage
                )

            filtered_samples.append(
                {
                    "timestamp_s": samples[index]["timestamp_s"],
                    "filtered_current_a": filtered_current,
                    "filtered_voltage_v": filtered_voltage,
                    "segment_start": segment_start,
                }
            )

            previous_current = filtered_current
            previous_voltage = filtered_voltage
            current_history.append(currents[index])
            voltage_history.append(voltages[index])
            max_previous = median_window - 1
            if len(current_history) > max_previous:
                current_history = current_history[-max_previous:] if max_previous else []
                voltage_history = voltage_history[-max_previous:] if max_previous else []

        return {
            "samples": filtered_samples,
            "segment_count": segment_count,
            "sample_count": len(samples),
        }

    def aggregate_telemetry(self, payload: Any) -> dict[str, Any]:
        """Aggregate telemetry into per-window energy buckets and a power trend.

        Fixed left-closed, right-open windows of ``bucket_duration_s``
        start at the first timestamp and end at the last. Adjacent
        samples are joined by linear power segments; a segment longer
        than ``max_gap_s`` is skipped entirely. Remaining segments are
        trapezoid-integrated per window, splitting at window boundaries
        and power zero crossings. Positive power counts as discharge
        energy, negative as charge energy. The trend is the ordinary
        least-squares slope of window average power against window
        midpoint hours.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        samples = payload.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            raise ApiError(
                422,
                "invalid_samples",
                "samples must be an array with at least two items",
            )
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(
                    422, "invalid_samples", "each sample must be an object"
                )

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            timestamps.append(float(timestamp))
            previous_timestamp = timestamp

        currents: list[float] = []
        voltages: list[float] = []
        for sample in samples:
            current = sample.get("current_a")
            voltage = sample.get("voltage_v")
            if not _is_finite_number(current) or not _is_finite_number(voltage):
                raise ApiError(
                    422,
                    "invalid_measurement",
                    "current_a and voltage_v must be finite numbers",
                )
            currents.append(float(current))
            voltages.append(float(voltage))

        bucket_duration, max_gap, trend_threshold = self._parse_aggregate_options(
            payload
        )

        powers = [currents[index] * voltages[index] for index in range(len(samples))]
        start_time = timestamps[0]
        end_time = timestamps[-1]
        bucket_count = max(1, math.ceil((end_time - start_time) / bucket_duration))
        covered = [0.0] * bucket_count
        discharge_ws = [0.0] * bucket_count
        charge_ws = [0.0] * bucket_count
        skipped_gap_count = 0

        for index in range(1, len(samples)):
            segment_start = timestamps[index - 1]
            segment_end = timestamps[index]
            if segment_end - segment_start > max_gap:
                skipped_gap_count += 1
                continue
            power_start = powers[index - 1]
            power_end = powers[index]
            span = segment_end - segment_start

            splits = [segment_start]
            boundary_index = (
                math.floor((segment_start - start_time) / bucket_duration) + 1
            )
            while True:
                boundary = start_time + boundary_index * bucket_duration
                if boundary >= segment_end:
                    break
                if boundary > segment_start:
                    splits.append(boundary)
                boundary_index += 1
            if power_start * power_end < 0.0:
                fraction = power_start / (power_start - power_end)
                splits.append(segment_start + fraction * span)
            splits.append(segment_end)
            splits.sort()

            for piece_start, piece_end in zip(splits, splits[1:]):
                if piece_end <= piece_start:
                    continue
                piece_power_start = power_start + (power_end - power_start) * (
                    piece_start - segment_start
                ) / span
                piece_power_end = power_start + (power_end - power_start) * (
                    piece_end - segment_start
                ) / span
                energy = (
                    (piece_power_start + piece_power_end)
                    / 2.0
                    * (piece_end - piece_start)
                )
                relative = (piece_start - start_time) / bucket_duration
                nearest = round(relative)
                if abs(relative - nearest) < 1e-9:
                    bucket_index = int(nearest)
                else:
                    bucket_index = int(math.floor(relative))
                covered[bucket_index] += piece_end - piece_start
                if energy > 0.0:
                    discharge_ws[bucket_index] += energy
                elif energy < 0.0:
                    charge_ws[bucket_index] -= energy

        buckets: list[dict[str, Any]] = []
        total_discharge_ws = 0.0
        total_charge_ws = 0.0
        for bucket_index in range(bucket_count):
            if covered[bucket_index] <= 0.0:
                continue
            bucket_start = start_time + bucket_index * bucket_duration
            bucket_end = min(
                start_time + (bucket_index + 1) * bucket_duration, end_time
            )
            discharge = discharge_ws[bucket_index] / 3600.0
            charge = charge_ws[bucket_index] / 3600.0
            net = discharge - charge
            average_power = net * 3600.0 / covered[bucket_index]
            buckets.append(
                {
                    "bucket_start_s": bucket_start,
                    "bucket_end_s": bucket_end,
                    "covered_duration_s": covered[bucket_index],
                    "average_power_w": average_power,
                    "discharge_energy_wh": discharge,
                    "charge_energy_wh": charge,
                    "net_energy_wh": net,
                }
            )
            total_discharge_ws += discharge_ws[bucket_index]
            total_charge_ws += charge_ws[bucket_index]

        total_discharge = total_discharge_ws / 3600.0
        total_charge = total_charge_ws / 3600.0

        trend_slope: float | None = None
        if len(buckets) < 2:
            trend_status = "insufficient"
        else:
            midpoint_hours = [
                (bucket["bucket_start_s"] + bucket["bucket_end_s"]) / 7200.0
                for bucket in buckets
            ]
            average_powers = [bucket["average_power_w"] for bucket in buckets]
            mean_x = sum(midpoint_hours) / len(midpoint_hours)
            mean_y = sum(average_powers) / len(average_powers)
            denominator = sum((x - mean_x) ** 2 for x in midpoint_hours)
            if denominator > 0.0:
                trend_slope = sum(
                    (x - mean_x) * (y - mean_y)
                    for x, y in zip(midpoint_hours, average_powers)
                ) / denominator
            else:
                trend_slope = 0.0
            if trend_slope > trend_threshold:
                trend_status = "increasing"
            elif trend_slope < -trend_threshold:
                trend_status = "decreasing"
            else:
                trend_status = "stable"

        return {
            "buckets": buckets,
            "discharge_energy_wh": total_discharge,
            "charge_energy_wh": total_charge,
            "net_energy_wh": total_discharge - total_charge,
            "skipped_gap_count": skipped_gap_count,
            "trend_slope_w_per_hour": trend_slope,
            "trend_status": trend_status,
        }

    TREND_LEVELS = ("normal", "warning", "critical")

    def analyze_telemetry_trend(self, payload: Any) -> dict[str, Any]:
        """Roll a median power baseline over aggregate windows with hysteresis.

        A window is usable only when it has positive covered duration and
        its coverage ratio reaches ``min_coverage_ratio``. Each usable
        window is scored against the median of the previous
        ``baseline_window`` usable powers (the window itself joins the
        history afterwards); until the history is full the target is
        ``warming_up``. Deviation at or above the critical/warning
        threshold targets ``critical``/``warning``. Escalation is
        immediate, while de-escalation needs ``recovery_windows``
        consecutive usable windows targeting a lower level, dropping a
        single level each time. Unusable windows target
        ``insufficient``, hold the level, reset the recovery streak and
        never enter the history.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        buckets = payload.get("buckets")
        if not isinstance(buckets, list) or not buckets:
            raise ApiError(
                422, "invalid_buckets", "buckets must be a non-empty array"
            )
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise ApiError(
                    422, "invalid_buckets", "each bucket must be an object"
                )

        starts: list[float] = []
        ends: list[float] = []
        coverages: list[float] = []
        powers: list[float] = []
        previous_start: float | None = None
        previous_end: float | None = None
        for bucket in buckets:
            start = bucket.get("bucket_start_s")
            end = bucket.get("bucket_end_s")
            covered = bucket.get("covered_duration_s")
            power = bucket.get("average_power_w")
            if (
                not _is_finite_number(start)
                or not _is_finite_number(end)
                or not _is_finite_number(covered)
                or not _is_finite_number(power)
            ):
                raise ApiError(
                    422,
                    "invalid_buckets",
                    "bucket_start_s, bucket_end_s, covered_duration_s and "
                    "average_power_w must be finite numbers",
                )
            if start >= end:
                raise ApiError(
                    422,
                    "invalid_buckets",
                    "bucket_start_s must be strictly before bucket_end_s",
                )
            if covered < 0.0 or covered > end - start:
                raise ApiError(
                    422,
                    "invalid_buckets",
                    "covered_duration_s must be within "
                    "[0, bucket_end_s - bucket_start_s]",
                )
            if previous_start is not None and start <= previous_start:
                raise ApiError(
                    422,
                    "invalid_buckets",
                    "bucket_start_s must be strictly increasing",
                )
            if previous_end is not None and start < previous_end:
                raise ApiError(
                    422, "invalid_buckets", "buckets must not overlap"
                )
            starts.append(float(start))
            ends.append(float(end))
            coverages.append(float(covered))
            powers.append(float(power))
            previous_start = start
            previous_end = end

        (
            baseline_window,
            recovery_windows,
            min_coverage_ratio,
            warning_deviation,
            critical_deviation,
        ) = self._parse_trend_config(payload.get("config"))

        level_names = self.TREND_LEVELS
        level = 0
        recovery_streak = 0
        history: list[float] = []
        results: list[dict[str, Any]] = []

        for index in range(len(buckets)):
            window_duration = ends[index] - starts[index]
            coverage_ratio = coverages[index] / window_duration
            usable = (
                coverages[index] > 0.0
                and coverage_ratio >= min_coverage_ratio
            )

            baseline: float | None = None
            deviation: float | None = None
            if not usable:
                target_name = "insufficient"
                recovery_streak = 0
            else:
                if history:
                    baseline = _median(history[-baseline_window:])
                    deviation = powers[index] - baseline
                if len(history) < baseline_window:
                    target_name = "warming_up"
                    recovery_streak = 0
                else:
                    if deviation >= critical_deviation:
                        target = 2
                    elif deviation >= warning_deviation:
                        target = 1
                    else:
                        target = 0
                    target_name = level_names[target]
                    if target > level:
                        level = target
                        recovery_streak = 0
                    elif target < level:
                        recovery_streak += 1
                        if recovery_streak >= recovery_windows:
                            level -= 1
                            recovery_streak = 0
                    else:
                        recovery_streak = 0
                history.append(powers[index])

            results.append(
                {
                    "bucket_start_s": buckets[index]["bucket_start_s"],
                    "bucket_end_s": buckets[index]["bucket_end_s"],
                    "covered_duration_s": buckets[index]["covered_duration_s"],
                    "coverage_ratio": coverage_ratio,
                    "average_power_w": buckets[index]["average_power_w"],
                    "baseline_power_w": baseline,
                    "deviation_w": deviation,
                    "target_level": target_name,
                    "level": level_names[level],
                }
            )

        return {"results": results, "final_level": level_names[level]}

    def estimate_dcdc_efficiency(self, payload: Any) -> dict[str, Any]:
        """Estimate per-operating-point DC-DC efficiency and energy totals."""
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        input_voltage = payload.get("input_voltage_v")
        if not _is_finite_number(input_voltage) or input_voltage <= 0:
            raise ApiError(
                422,
                "invalid_input_voltage",
                "input_voltage_v must be a positive finite number",
            )

        quiescent_current = payload.get("quiescent_current_a", 0.0)
        if not _is_finite_number(quiescent_current) or quiescent_current < 0:
            raise ApiError(
                422,
                "invalid_quiescent_current",
                "quiescent_current_a must be a non-negative finite number",
            )

        operating_points = payload.get("operating_points")
        if not isinstance(operating_points, list) or not operating_points:
            raise ApiError(
                422,
                "invalid_operating_points",
                "operating_points must be a non-empty array",
            )
        for point in operating_points:
            if not isinstance(point, dict):
                raise ApiError(
                    422,
                    "invalid_operating_points",
                    "each operating point must be an object",
                )

        curve = self._parse_efficiency_curve(payload.get("efficiency_curve"))

        durations: list[float] = []
        output_voltages: list[float] = []
        output_currents: list[float] = []
        for point in operating_points:
            duration = point.get("duration_s")
            output_voltage = point.get("output_voltage_v")
            output_current = point.get("output_current_a")
            if (
                not _is_finite_number(duration)
                or duration <= 0
                or not _is_finite_number(output_voltage)
                or output_voltage <= 0
                or not _is_finite_number(output_current)
                or output_current < 0
            ):
                raise ApiError(
                    422,
                    "invalid_operating_point",
                    "duration_s and output_voltage_v must be positive finite "
                    "numbers and output_current_a must be a non-negative "
                    "finite number",
                )
            durations.append(float(duration))
            output_voltages.append(float(output_voltage))
            output_currents.append(float(output_current))

        quiescent_power = float(input_voltage) * float(quiescent_current)
        estimates: list[dict[str, Any]] = []
        input_energy_wh = 0.0
        output_energy_wh = 0.0
        loss_energy_wh = 0.0

        for index in range(len(operating_points)):
            output_power = output_voltages[index] * output_currents[index]
            efficiency = _interpolate_efficiency(output_currents[index], curve)
            input_power = output_power / efficiency + quiescent_power
            loss_power = input_power - output_power
            returned_efficiency = (
                output_power / input_power if input_power != 0.0 else 0.0
            )
            estimates.append(
                {
                    "input_power_w": input_power,
                    "output_power_w": output_power,
                    "loss_power_w": loss_power,
                    "efficiency": returned_efficiency,
                }
            )
            input_energy_wh += input_power * durations[index] / 3600.0
            output_energy_wh += output_power * durations[index] / 3600.0
            loss_energy_wh += loss_power * durations[index] / 3600.0

        overall_efficiency = (
            output_energy_wh / input_energy_wh if input_energy_wh != 0.0 else 0.0
        )

        return {
            "estimates": estimates,
            "input_energy_wh": input_energy_wh,
            "output_energy_wh": output_energy_wh,
            "loss_energy_wh": loss_energy_wh,
            "overall_efficiency": overall_efficiency,
        }

    def estimate_ldo_efficiency(self, payload: Any) -> dict[str, Any]:
        """Estimate per-operating-point LDO efficiency and energy totals."""
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        operating_points = payload.get("operating_points")
        if not isinstance(operating_points, list) or not operating_points:
            raise ApiError(
                422,
                "invalid_operating_points",
                "operating_points must be a non-empty array",
            )
        for point in operating_points:
            if not isinstance(point, dict):
                raise ApiError(
                    422,
                    "invalid_operating_points",
                    "each operating point must be an object",
                )

        dropout_curve = self._parse_dropout_curve(payload.get("dropout_curve"))

        quiescent_current = payload.get("quiescent_current_a", 0.0)
        if not _is_finite_number(quiescent_current) or quiescent_current < 0:
            raise ApiError(
                422,
                "invalid_quiescent_current",
                "quiescent_current_a must be a non-negative finite number",
            )

        durations: list[float] = []
        input_voltages: list[float] = []
        requested_voltages: list[float] = []
        output_currents: list[float] = []
        for point in operating_points:
            duration = point.get("duration_s")
            input_voltage = point.get("input_voltage_v")
            requested_voltage = point.get("requested_output_voltage_v")
            output_current = point.get("output_current_a")
            if (
                not _is_finite_number(duration)
                or duration <= 0
                or not _is_finite_number(input_voltage)
                or input_voltage <= 0
                or not _is_finite_number(requested_voltage)
                or requested_voltage <= 0
                or not _is_finite_number(output_current)
                or output_current < 0
            ):
                raise ApiError(
                    422,
                    "invalid_operating_point",
                    "duration_s, input_voltage_v and requested_output_voltage_v "
                    "must be positive finite numbers and output_current_a must "
                    "be a non-negative finite number",
                )
            durations.append(float(duration))
            input_voltages.append(float(input_voltage))
            requested_voltages.append(float(requested_voltage))
            output_currents.append(float(output_current))

        estimates: list[dict[str, Any]] = []
        input_energy_wh = 0.0
        output_energy_wh = 0.0
        loss_energy_wh = 0.0

        for index in range(len(operating_points)):
            dropout_voltage = _interpolate_dropout(
                output_currents[index], dropout_curve
            )
            maximum_output_voltage = max(
                0.0, input_voltages[index] - dropout_voltage
            )
            actual_output_voltage = min(
                maximum_output_voltage, requested_voltages[index]
            )
            state = (
                "regulated"
                if maximum_output_voltage >= requested_voltages[index]
                else "dropout"
            )
            output_power = actual_output_voltage * output_currents[index]
            input_power = input_voltages[index] * (
                output_currents[index] + float(quiescent_current)
            )
            loss_power = input_power - output_power
            efficiency = output_power / input_power if input_power != 0.0 else 0.0
            estimates.append(
                {
                    "actual_output_voltage_v": actual_output_voltage,
                    "dropout_voltage_v": dropout_voltage,
                    "input_power_w": input_power,
                    "output_power_w": output_power,
                    "loss_power_w": loss_power,
                    "efficiency": efficiency,
                    "state": state,
                }
            )
            input_energy_wh += input_power * durations[index] / 3600.0
            output_energy_wh += output_power * durations[index] / 3600.0
            loss_energy_wh += loss_power * durations[index] / 3600.0

        overall_efficiency = (
            output_energy_wh / input_energy_wh if input_energy_wh != 0.0 else 0.0
        )

        return {
            "estimates": estimates,
            "input_energy_wh": input_energy_wh,
            "output_energy_wh": output_energy_wh,
            "loss_energy_wh": loss_energy_wh,
            "overall_efficiency": overall_efficiency,
        }

    def estimate_solar_harvest(self, payload: Any) -> dict[str, Any]:
        """Estimate solar harvest per sample and integrate energy totals.

        Panel power is clipped to the harvester input limit, converted at
        the interpolated curve efficiency, then clipped to the battery
        acceptance limit. Adjacent samples are trapezoid-integrated.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        samples = payload.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            raise ApiError(
                422,
                "invalid_samples",
                "samples must be an array with at least two items",
            )
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(
                    422, "invalid_samples", "each sample must be an object"
                )

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_samples", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_samples", "timestamp_s must be strictly increasing"
                )
            timestamps.append(float(timestamp))
            previous_timestamp = timestamp

        panel_voltages: list[float] = []
        panel_currents: list[float] = []
        acceptance_powers: list[float] = []
        for sample in samples:
            panel_voltage = sample.get("panel_voltage_v")
            panel_current = sample.get("panel_current_a")
            acceptance_power = sample.get("battery_acceptance_power_w")
            if (
                not _is_finite_number(panel_voltage)
                or panel_voltage < 0
                or not _is_finite_number(panel_current)
                or panel_current < 0
                or not _is_finite_number(acceptance_power)
                or acceptance_power < 0
            ):
                raise ApiError(
                    422,
                    "invalid_samples",
                    "panel_voltage_v, panel_current_a and "
                    "battery_acceptance_power_w must be non-negative finite "
                    "numbers",
                )
            panel_voltages.append(float(panel_voltage))
            panel_currents.append(float(panel_current))
            acceptance_powers.append(float(acceptance_power))

        max_input_power, curve = self._parse_harvester_config(
            payload.get("harvester")
        )

        estimates: list[dict[str, Any]] = []
        for index in range(len(samples)):
            available_power = panel_voltages[index] * panel_currents[index]
            harvester_input_power = min(available_power, max_input_power)
            efficiency = _interpolate_efficiency(harvester_input_power, curve)
            converted_power = harvester_input_power * efficiency
            harvested_power = min(converted_power, acceptance_powers[index])
            estimates.append(
                {
                    "timestamp_s": samples[index]["timestamp_s"],
                    "available_power_w": available_power,
                    "harvester_input_power_w": harvester_input_power,
                    "efficiency": efficiency,
                    "converted_power_w": converted_power,
                    "harvested_power_w": harvested_power,
                    "conversion_loss_power_w": harvester_input_power
                    - converted_power,
                    "curtailed_power_w": available_power - harvester_input_power,
                    "rejected_power_w": converted_power - harvested_power,
                }
            )

        power_keys = (
            "available_power_w",
            "harvested_power_w",
            "conversion_loss_power_w",
            "curtailed_power_w",
            "rejected_power_w",
        )
        energy: dict[str, float] = {}
        for power_key in power_keys:
            prefix = power_key[: -len("_power_w")]
            total = 0.0
            for index in range(1, len(estimates)):
                delta_time = timestamps[index] - timestamps[index - 1]
                average_power = (
                    estimates[index - 1][power_key] + estimates[index][power_key]
                ) / 2.0
                total += average_power * delta_time / 3600.0
            energy[f"{prefix}_energy_wh"] = total

        available_energy = energy["available_energy_wh"]
        overall_efficiency = (
            energy["harvested_energy_wh"] / available_energy
            if available_energy != 0.0
            else 0.0
        )

        return {
            "estimates": estimates,
            "available_energy_wh": energy["available_energy_wh"],
            "harvested_energy_wh": energy["harvested_energy_wh"],
            "conversion_loss_energy_wh": energy["conversion_loss_energy_wh"],
            "curtailed_energy_wh": energy["curtailed_energy_wh"],
            "rejected_energy_wh": energy["rejected_energy_wh"],
            "overall_efficiency": overall_efficiency,
        }

    def protect_thermal(self, payload: Any) -> dict[str, Any]:
        """Limit charge current from cell temperature with a latching cutoff.

        Below the warning threshold the thermal limit is the configured
        maximum; between warning and critical it falls linearly to zero;
        at or above critical the limit is zero and the cutoff latches.
        The latch releases only once the temperature drops to the
        recovery threshold or lower, where zone-based limiting resumes.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        protection = payload.get("protection")
        if not isinstance(protection, dict):
            raise ApiError(
                422,
                "invalid_protection_config",
                "protection must be an object",
            )

        max_charge_current = protection.get("max_charge_current_a")
        if not _is_finite_number(max_charge_current) or max_charge_current <= 0:
            raise ApiError(
                422,
                "invalid_protection_config",
                "max_charge_current_a must be a positive finite number",
            )

        recovery_temperature = protection.get("recovery_temperature_c")
        warning_temperature = protection.get("warning_temperature_c")
        critical_temperature = protection.get("critical_temperature_c")
        if (
            not _is_finite_number(recovery_temperature)
            or not _is_finite_number(warning_temperature)
            or not _is_finite_number(critical_temperature)
        ):
            raise ApiError(
                422,
                "invalid_protection_config",
                "recovery_temperature_c, warning_temperature_c and "
                "critical_temperature_c must be finite numbers",
            )
        if not recovery_temperature < warning_temperature < critical_temperature:
            raise ApiError(
                422,
                "invalid_protection_config",
                "thresholds must satisfy recovery_temperature_c < "
                "warning_temperature_c < critical_temperature_c",
            )

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "invalid_samples", "samples must be a non-empty array")
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(422, "invalid_samples", "each sample must be an object")

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            timestamps.append(timestamp)
            previous_timestamp = timestamp

        temperatures: list[float] = []
        for sample in samples:
            temperature = sample.get("temperature_c")
            if not _is_finite_number(temperature):
                raise ApiError(
                    422, "invalid_temperature", "temperature_c must be a finite number"
                )
            temperatures.append(float(temperature))

        requested_currents: list[float] = []
        for sample in samples:
            requested_current = sample.get("requested_current_a")
            if not _is_finite_number(requested_current) or requested_current < 0:
                raise ApiError(
                    422,
                    "invalid_current",
                    "requested_current_a must be a non-negative finite number",
                )
            requested_currents.append(float(requested_current))

        max_charge_current = float(max_charge_current)
        warning_span = float(critical_temperature) - float(warning_temperature)

        decisions: list[dict[str, Any]] = []
        latched = False
        cutoff_count = 0
        for index in range(len(samples)):
            temperature = temperatures[index]
            if latched:
                if temperature <= recovery_temperature:
                    latched = False
                else:
                    thermal_limit = 0.0
                    state = "cutoff"
            if not latched:
                if temperature >= critical_temperature:
                    thermal_limit = 0.0
                    state = "cutoff"
                    latched = True
                    cutoff_count += 1
                elif temperature >= warning_temperature:
                    thermal_limit = (
                        max_charge_current
                        * (float(critical_temperature) - temperature)
                        / warning_span
                    )
                    state = "derated"
                else:
                    thermal_limit = max_charge_current
                    state = "normal"
            allowed_current = min(requested_currents[index], thermal_limit)
            decisions.append(
                {
                    "timestamp_s": samples[index]["timestamp_s"],
                    "allowed_current_a": allowed_current,
                    "thermal_limit_a": thermal_limit,
                    "state": state,
                }
            )

        return {
            "decisions": decisions,
            "final_state": decisions[-1]["state"],
            "cutoff_count": cutoff_count,
        }

    def plan_balance(self, payload: Any) -> dict[str, Any]:
        """Plan passive cell balancing with hysteresis and channel limits.

        Cells below the maximum temperature become candidates when a
        previously inactive cell reaches ``start_delta_v`` or a
        previously active cell stays strictly above ``stop_delta_v``;
        cells at or above the maximum temperature never balance. Up to
        ``max_channels`` candidates are activated by descending delta
        and ascending id.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        cells = payload.get("cells")
        if not isinstance(cells, list) or not cells:
            raise ApiError(422, "invalid_cells", "cells must be a non-empty array")
        for cell in cells:
            if not isinstance(cell, dict):
                raise ApiError(422, "invalid_cells", "each cell must be an object")

        ids: list[str] = []
        voltages: list[float] = []
        temperatures: list[float] = []
        seen_ids: set[str] = set()
        for cell in cells:
            cell_id = cell.get("id")
            if not isinstance(cell_id, str) or not cell_id:
                raise ApiError(
                    422, "invalid_cell_id", "each cell id must be a non-empty string"
                )
            if cell_id in seen_ids:
                raise ApiError(
                    422, "invalid_cell_id", f"cell id {cell_id!r} is duplicated"
                )
            seen_ids.add(cell_id)
            ids.append(cell_id)

            voltage = cell.get("voltage_v")
            if not _is_finite_number(voltage) or voltage <= 0:
                raise ApiError(
                    422,
                    "invalid_cell_voltage",
                    "voltage_v must be a positive finite number",
                )
            voltages.append(float(voltage))

            temperature = cell.get("temperature_c")
            if not _is_finite_number(temperature):
                raise ApiError(
                    422,
                    "invalid_cell_temperature",
                    "temperature_c must be a finite number",
                )
            temperatures.append(float(temperature))

        (
            start_delta,
            bleed_current,
            stop_delta,
            max_temperature,
            max_channels,
        ) = self._parse_balance_config(payload.get("config"), len(cells))

        if "previous_active_ids" in payload:
            previous_active = self._parse_previous_active_ids(
                payload["previous_active_ids"], ids
            )
        else:
            previous_active = set()

        target_voltage = min(voltages)
        deltas = [voltage - target_voltage for voltage in voltages]

        candidate_indexes: list[int] = []
        blocked = [False] * len(cells)
        eligible = [False] * len(cells)
        for index in range(len(cells)):
            if temperatures[index] >= max_temperature:
                blocked[index] = True
            elif ids[index] in previous_active:
                if deltas[index] > stop_delta:
                    eligible[index] = True
                    candidate_indexes.append(index)
            elif deltas[index] >= start_delta:
                eligible[index] = True
                candidate_indexes.append(index)

        candidate_indexes.sort(key=lambda i: (-deltas[i], ids[i]))
        chosen_indexes = candidate_indexes[:max_channels]
        selected = set(chosen_indexes)
        active_ids = [ids[index] for index in chosen_indexes]

        decisions: list[dict[str, Any]] = []
        for index in range(len(cells)):
            if blocked[index]:
                active = False
                assigned_current = 0.0
                reason = "temperature_blocked"
            elif index in selected:
                active = True
                assigned_current = bleed_current
                reason = "selected"
            elif eligible[index]:
                active = False
                assigned_current = 0.0
                reason = "channel_limited"
            else:
                active = False
                assigned_current = 0.0
                reason = "below_threshold"
            decisions.append(
                {
                    "id": ids[index],
                    "delta_voltage_v": deltas[index],
                    "active": active,
                    "bleed_current_a": assigned_current,
                    "reason": reason,
                }
            )

        return {
            "target_voltage_v": target_voltage,
            "decisions": decisions,
            "active_ids": active_ids,
            "status": "balancing" if active_ids else "idle",
        }

    def plan_charging(self, payload: Any) -> dict[str, Any]:
        """Plan a fixed-step constant-current/taper charge to a target SoC.

        While SoC is at or below ``taper_start_soc`` the pack takes
        ``max_current_a``; above it the current falls linearly with the
        remaining SoC distance to ``taper_end_current_a`` at the target.
        Each step advances by ``current_a * coulombic_efficiency *
        duration_s / (capacity_ah * 3600)``. A step that would cross the
        target is shortened so the final value lands exactly on it.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        capacity = payload.get("capacity_ah")
        if not _is_finite_number(capacity) or capacity <= 0:
            raise ApiError(
                422, "invalid_capacity", "capacity_ah must be a positive finite number"
            )

        initial_soc = payload.get("initial_soc")
        target_soc = payload.get("target_soc")
        if (
            not _is_finite_number(initial_soc)
            or not _is_finite_number(target_soc)
            or not 0 <= initial_soc <= 1
            or not 0 <= target_soc <= 1
            or initial_soc > target_soc
        ):
            raise ApiError(
                422,
                "invalid_soc_range",
                "initial_soc and target_soc must be finite numbers within "
                "[0, 1] with initial_soc <= target_soc",
            )

        (
            max_current,
            charge_voltage,
            coulombic_efficiency,
            taper_start_soc,
            taper_end_current,
        ) = self._parse_charge_config(payload.get("config"), float(target_soc))

        step_duration, max_steps = self._parse_charge_plan_options(payload)

        capacity = float(capacity)
        initial_soc = float(initial_soc)
        target_soc = float(target_soc)
        coulomb_seconds = capacity * 3600.0
        taper_span = target_soc - taper_start_soc
        current_span = max_current - taper_end_current

        def current_at(soc: float) -> float:
            if soc <= taper_start_soc:
                return max_current
            fraction = (soc - taper_start_soc) / taper_span
            return max_current - current_span * fraction

        steps: list[dict[str, Any]] = []
        soc = initial_soc
        elapsed = 0.0
        total_energy = 0.0
        completed = soc >= target_soc
        if not completed:
            for _ in range(max_steps):
                current = current_at(soc)
                full_delta = (
                    current
                    * coulombic_efficiency
                    * step_duration
                    / coulomb_seconds
                )
                duration = step_duration
                if soc + full_delta >= target_soc:
                    duration = (target_soc - soc) * coulomb_seconds / (
                        current * coulombic_efficiency
                    )
                    end_soc = target_soc
                else:
                    end_soc = soc + full_delta
                energy = charge_voltage * current * duration / 3600.0
                steps.append(
                    {
                        "start_soc": soc,
                        "end_soc": end_soc,
                        "duration_s": duration,
                        "current_a": current,
                        "input_energy_wh": energy,
                    }
                )
                soc = end_soc
                elapsed += duration
                total_energy += energy
                if soc >= target_soc:
                    completed = True
                    break

        return {
            "steps": steps,
            "final_soc": soc,
            "elapsed_s": elapsed,
            "input_energy_wh": total_energy,
            "status": "completed" if completed else "incomplete",
        }

    def dispatch_parallel_packs(self, payload: Any) -> dict[str, Any]:
        """Dispatch bus current across parallel battery packs with fault isolation.

        All packs start connected. A pack is isolated at the current sample
        when its fault flag is set or its voltage deviates from the bus
        voltage by more than ``max_bus_voltage_delta_v``; isolated packs are
        allocated zero current. An isolated pack reconnects at the last of
        ``recovery_samples`` consecutive safe samples; an unsafe sample
        resets that streak. The bus request is shared equally among
        connected packs, capping each at its own maximum and redistributing
        the remainder equally among the rest until the request is met or
        every connected pack is at its cap.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        config = payload.get("config")
        if not isinstance(config, dict):
            raise ApiError(
                422, "invalid_parallel_config", "config must be an object"
            )

        max_voltage_delta = config.get("max_bus_voltage_delta_v")
        if not _is_finite_number(max_voltage_delta) or max_voltage_delta < 0:
            raise ApiError(
                422,
                "invalid_parallel_config",
                "max_bus_voltage_delta_v must be a non-negative finite number",
            )

        recovery_samples = config.get("recovery_samples")
        if (
            not isinstance(recovery_samples, int)
            or isinstance(recovery_samples, bool)
            or recovery_samples < 1
        ):
            raise ApiError(
                422,
                "invalid_parallel_config",
                "recovery_samples must be a positive integer",
            )

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(
                422, "invalid_samples", "samples must be a non-empty array"
            )
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(
                    422, "invalid_samples", "each sample must be an object"
                )

        timestamps: list[float] = []
        requested_currents: list[float] = []
        bus_voltages: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_samples", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422,
                    "invalid_samples",
                    "timestamp_s must be strictly increasing",
                )
            requested = sample.get("requested_bus_current_a")
            if not _is_finite_number(requested) or requested < 0:
                raise ApiError(
                    422,
                    "invalid_samples",
                    "requested_bus_current_a must be a non-negative finite number",
                )
            bus_voltage = sample.get("bus_voltage_v")
            if not _is_finite_number(bus_voltage) or bus_voltage <= 0:
                raise ApiError(
                    422,
                    "invalid_samples",
                    "bus_voltage_v must be a positive finite number",
                )
            timestamps.append(float(timestamp))
            requested_currents.append(float(requested))
            bus_voltages.append(float(bus_voltage))
            previous_timestamp = timestamp

        sample_pack_ids: list[list[str]] = []
        sample_pack_voltages: list[list[float]] = []
        sample_pack_max_currents: list[list[float]] = []
        sample_pack_faults: list[list[bool]] = []
        reference_ids: set[str] | None = None
        for sample in samples:
            packs = sample.get("packs")
            if not isinstance(packs, list) or not packs:
                raise ApiError(
                    422, "invalid_packs", "packs must be a non-empty array"
                )
            ids: list[str] = []
            voltages: list[float] = []
            max_currents: list[float] = []
            faults: list[bool] = []
            seen_ids: set[str] = set()
            for pack in packs:
                if not isinstance(pack, dict):
                    raise ApiError(
                        422, "invalid_packs", "each pack must be an object"
                    )
                pack_id = pack.get("id")
                if not isinstance(pack_id, str) or not pack_id:
                    raise ApiError(
                        422,
                        "invalid_packs",
                        "each pack id must be a non-empty string",
                    )
                if pack_id in seen_ids:
                    raise ApiError(
                        422,
                        "invalid_packs",
                        f"pack id {pack_id!r} is duplicated",
                    )
                seen_ids.add(pack_id)
                voltage = pack.get("voltage_v")
                if not _is_finite_number(voltage) or voltage <= 0:
                    raise ApiError(
                        422,
                        "invalid_packs",
                        "voltage_v must be a positive finite number",
                    )
                max_current = pack.get("max_discharge_current_a")
                if not _is_finite_number(max_current) or max_current < 0:
                    raise ApiError(
                        422,
                        "invalid_packs",
                        "max_discharge_current_a must be a non-negative "
                        "finite number",
                    )
                fault = pack.get("fault")
                if not isinstance(fault, bool):
                    raise ApiError(
                        422, "invalid_packs", "fault must be a boolean"
                    )
                ids.append(pack_id)
                voltages.append(float(voltage))
                max_currents.append(float(max_current))
                faults.append(fault)
            if reference_ids is None:
                reference_ids = set(ids)
            elif set(ids) != reference_ids:
                raise ApiError(
                    422,
                    "invalid_packs",
                    "each sample must carry the same set of pack ids",
                )
            sample_pack_ids.append(ids)
            sample_pack_voltages.append(voltages)
            sample_pack_max_currents.append(max_currents)
            sample_pack_faults.append(faults)

        max_voltage_delta = float(max_voltage_delta)
        connected: dict[str, bool] = {
            pack_id: True for pack_id in sample_pack_ids[0]
        }
        safe_streak: dict[str, int] = {
            pack_id: 0 for pack_id in sample_pack_ids[0]
        }

        decisions: list[dict[str, Any]] = []
        for index in range(len(samples)):
            ids = sample_pack_ids[index]
            voltages = sample_pack_voltages[index]
            max_currents = sample_pack_max_currents[index]
            faults = sample_pack_faults[index]
            bus_voltage = bus_voltages[index]
            request = requested_currents[index]

            states: dict[str, bool] = {}
            caps: dict[str, float] = {}
            for position, pack_id in enumerate(ids):
                unsafe = (
                    faults[position]
                    or abs(voltages[position] - bus_voltage) > max_voltage_delta
                )
                if connected[pack_id]:
                    if unsafe:
                        connected[pack_id] = False
                        safe_streak[pack_id] = 0
                else:
                    if unsafe:
                        safe_streak[pack_id] = 0
                    else:
                        safe_streak[pack_id] += 1
                        if safe_streak[pack_id] >= recovery_samples:
                            connected[pack_id] = True
                            safe_streak[pack_id] = 0
                states[pack_id] = connected[pack_id]
                caps[pack_id] = max_currents[position]

            allocation: dict[str, float] = {pack_id: 0.0 for pack_id in ids}
            active = [pack_id for pack_id in ids if states[pack_id]]
            remaining = request
            while active and remaining > 0.0:
                share = remaining / len(active)
                still_active: list[str] = []
                for pack_id in active:
                    room = caps[pack_id] - allocation[pack_id]
                    if room <= share:
                        allocation[pack_id] = caps[pack_id]
                        remaining -= room
                    else:
                        still_active.append(pack_id)
                if len(still_active) == len(active):
                    for pack_id in active:
                        allocation[pack_id] += share
                    remaining = 0.0
                else:
                    active = still_active

            allocated = sum(allocation[pack_id] for pack_id in ids)
            unmet = request - allocated
            if unmet < 0.0 or unmet <= 1e-9 * max(request, 1.0):
                unmet = 0.0
                allocated = request

            if unmet == 0.0:
                status = "satisfied"
            elif request > 0.0 and not any(states[pack_id] for pack_id in ids):
                status = "no_available_pack"
            else:
                status = "constrained"

            pack_decisions = [
                {
                    "id": pack_id,
                    "allocated_current_a": allocation[pack_id],
                    "state": "connected" if states[pack_id] else "isolated",
                }
                for pack_id in ids
            ]
            decisions.append(
                {
                    "timestamp_s": timestamps[index],
                    "pack_decisions": pack_decisions,
                    "allocated_bus_current_a": allocated,
                    "unmet_bus_current_a": unmet,
                    "status": status,
                }
            )

        return {"decisions": decisions}

    def negotiate_wireless_charging(self, payload: Any) -> dict[str, Any]:
        """Negotiate wireless charging power across telemetry samples.

        Each sample picks a charge profile whose delivery cap — the
        smallest of the profile power and both end limits, scaled by
        coupling and a temperature derating factor — best fits the
        request: the smallest profile that satisfies it, else the one
        with the highest cap. A foreign object or a coil temperature at
        the critical threshold latches a fault that stops transmission
        until a later sample shows the receiver absent, no foreign
        object and a temperature at or below the recovery threshold.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        config = payload.get("config")
        if not isinstance(config, dict):
            raise ApiError(
                422, "invalid_wireless_config", "config must be an object"
            )

        transmitter_limit = config.get("transmitter_max_power_w")
        if not _is_finite_number(transmitter_limit) or transmitter_limit <= 0:
            raise ApiError(
                422,
                "invalid_wireless_config",
                "transmitter_max_power_w must be a positive finite number",
            )
        receiver_limit = config.get("receiver_max_power_w")
        if not _is_finite_number(receiver_limit) or receiver_limit <= 0:
            raise ApiError(
                422,
                "invalid_wireless_config",
                "receiver_max_power_w must be a positive finite number",
            )

        recovery_temperature = config.get("recovery_temperature_c")
        warning_temperature = config.get("warning_temperature_c")
        critical_temperature = config.get("critical_temperature_c")
        if (
            not _is_finite_number(recovery_temperature)
            or not _is_finite_number(warning_temperature)
            or not _is_finite_number(critical_temperature)
        ):
            raise ApiError(
                422,
                "invalid_wireless_config",
                "recovery_temperature_c, warning_temperature_c and "
                "critical_temperature_c must be finite numbers",
            )
        if not recovery_temperature < warning_temperature < critical_temperature:
            raise ApiError(
                422,
                "invalid_wireless_config",
                "thresholds must satisfy recovery_temperature_c < "
                "warning_temperature_c < critical_temperature_c",
            )

        profiles = config.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            raise ApiError(
                422, "invalid_profiles", "profiles must be a non-empty array"
            )
        profile_ids: list[str] = []
        profile_powers: list[float] = []
        profile_accepted: list[bool] = []
        seen_ids: set[str] = set()
        for profile in profiles:
            if not isinstance(profile, dict):
                raise ApiError(
                    422, "invalid_profiles", "each profile must be an object"
                )
            profile_id = profile.get("id")
            if not isinstance(profile_id, str) or not profile_id:
                raise ApiError(
                    422,
                    "invalid_profiles",
                    "each profile id must be a non-empty string",
                )
            if profile_id in seen_ids:
                raise ApiError(
                    422,
                    "invalid_profiles",
                    f"profile id {profile_id!r} is duplicated",
                )
            seen_ids.add(profile_id)
            voltage = profile.get("voltage_v")
            if not _is_finite_number(voltage) or voltage <= 0:
                raise ApiError(
                    422,
                    "invalid_profiles",
                    "voltage_v must be a positive finite number",
                )
            max_current = profile.get("max_current_a")
            if not _is_finite_number(max_current) or max_current <= 0:
                raise ApiError(
                    422,
                    "invalid_profiles",
                    "max_current_a must be a positive finite number",
                )
            accepted = profile.get("accepted")
            if not isinstance(accepted, bool):
                raise ApiError(
                    422, "invalid_profiles", "accepted must be a boolean"
                )
            profile_ids.append(profile_id)
            profile_powers.append(float(voltage) * float(max_current))
            profile_accepted.append(accepted)
        if not any(profile_accepted):
            raise ApiError(
                422,
                "invalid_profiles",
                "at least one profile must be acceptable",
            )

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "invalid_samples", "samples must be a non-empty array")
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(422, "invalid_samples", "each sample must be an object")

        timestamps: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            timestamps.append(timestamp)
            previous_timestamp = timestamp

        receiver_present: list[bool] = []
        requested_powers: list[float] = []
        couplings: list[float] = []
        temperatures: list[float] = []
        foreign_objects: list[bool] = []
        for sample in samples:
            present = sample.get("receiver_present")
            if not isinstance(present, bool):
                raise ApiError(
                    422,
                    "invalid_wireless_sample",
                    "receiver_present must be a boolean",
                )
            requested = sample.get("requested_power_w")
            if not _is_finite_number(requested) or requested < 0:
                raise ApiError(
                    422,
                    "invalid_wireless_sample",
                    "requested_power_w must be a non-negative finite number",
                )
            coupling = sample.get("coupling")
            if not _is_finite_number(coupling) or not 0 <= coupling <= 1:
                raise ApiError(
                    422,
                    "invalid_wireless_sample",
                    "coupling must be a finite number within [0, 1]",
                )
            temperature = sample.get("coil_temperature_c")
            if not _is_finite_number(temperature):
                raise ApiError(
                    422,
                    "invalid_wireless_sample",
                    "coil_temperature_c must be a finite number",
                )
            foreign_object = sample.get("foreign_object")
            if not isinstance(foreign_object, bool):
                raise ApiError(
                    422,
                    "invalid_wireless_sample",
                    "foreign_object must be a boolean",
                )
            receiver_present.append(present)
            requested_powers.append(float(requested))
            couplings.append(float(coupling))
            temperatures.append(float(temperature))
            foreign_objects.append(foreign_object)

        power_limit = min(float(transmitter_limit), float(receiver_limit))
        warning_span = float(critical_temperature) - float(warning_temperature)
        candidates = [
            index for index in range(len(profiles)) if profile_accepted[index]
        ]

        decisions: list[dict[str, Any]] = []
        latched = False
        latch_reason: str | None = None
        fault_count = 0
        for index in range(len(samples)):
            temperature = temperatures[index]
            if temperature <= warning_temperature:
                thermal_factor = 1.0
            elif temperature >= critical_temperature:
                thermal_factor = 0.0
            else:
                thermal_factor = (
                    float(critical_temperature) - temperature
                ) / warning_span

            if latched:
                if (
                    not receiver_present[index]
                    and not foreign_objects[index]
                    and temperature <= recovery_temperature
                ):
                    latched = False
                    latch_reason = None
            if not latched and (
                foreign_objects[index] or temperature >= critical_temperature
            ):
                latched = True
                latch_reason = (
                    "foreign_object"
                    if foreign_objects[index]
                    else "over_temperature"
                )
                fault_count += 1

            selected_profile_id: str | None = None
            delivered = 0.0
            if latched:
                state = "fault"
                fault_reason = latch_reason
            else:
                fault_reason = None
                if not receiver_present[index] or requested_powers[index] == 0.0:
                    state = "idle"
                else:
                    requested = requested_powers[index]
                    caps = {
                        candidate: min(profile_powers[candidate], power_limit)
                        * couplings[index]
                        * thermal_factor
                        for candidate in candidates
                    }
                    satisfying = [
                        candidate
                        for candidate in candidates
                        if caps[candidate] >= requested
                    ]
                    if satisfying:
                        chosen = min(
                            satisfying,
                            key=lambda c: (profile_powers[c], profile_ids[c]),
                        )
                    else:
                        chosen = min(
                            candidates,
                            key=lambda c: (-caps[c], profile_powers[c], profile_ids[c]),
                        )
                    selected_profile_id = profile_ids[chosen]
                    delivered = min(requested, caps[chosen])
                    state = "charging" if delivered >= requested else "limited"

            unmet = requested_powers[index] - delivered
            if unmet < 0.0:
                unmet = 0.0
            decisions.append(
                {
                    "timestamp_s": samples[index]["timestamp_s"],
                    "selected_profile_id": selected_profile_id,
                    "delivered_power_w": delivered,
                    "unmet_power_w": unmet,
                    "thermal_factor": thermal_factor,
                    "state": state,
                    "fault_reason": fault_reason,
                }
            )

        return {
            "decisions": decisions,
            "final_state": decisions[-1]["state"],
            "fault_count": fault_count,
        }

    def compare_energy_benchmark(self, payload: Any) -> dict[str, Any]:
        """Compare historical baseline runs against current runs by power.

        Run power is ``energy_wh * 3600 / duration_s``; each group is
        summarized by its arithmetic mean. A current mean strictly above
        ``baseline * (1 + threshold/100)`` is a regression, strictly below
        ``baseline * (1 - threshold/100)`` an improvement, otherwise stable.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        scenarios = payload.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise ApiError(
                422, "invalid_scenarios", "scenarios must be a non-empty array"
            )
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise ApiError(
                    422, "invalid_scenarios", "each scenario must be an object"
                )

        threshold = payload.get(
            "regression_threshold_percent", DEFAULT_REGRESSION_THRESHOLD_PERCENT
        )
        if not _is_finite_number(threshold) or threshold < 0:
            raise ApiError(
                422,
                "invalid_options",
                "regression_threshold_percent must be a non-negative finite number",
            )
        threshold = float(threshold)

        seen_ids: set[str] = set()
        results: list[dict[str, Any]] = []
        regression_count = 0
        improvement_count = 0
        for scenario in scenarios:
            scenario_id = scenario.get("id")
            if not isinstance(scenario_id, str) or not scenario_id:
                raise ApiError(
                    422,
                    "invalid_scenario_id",
                    "each scenario id must be a non-empty string",
                )
            if scenario_id in seen_ids:
                raise ApiError(
                    422,
                    "invalid_scenario_id",
                    f"scenario id {scenario_id!r} is duplicated",
                )
            seen_ids.add(scenario_id)

            baseline_power = self._average_run_power(scenario.get("baseline_runs"))
            current_power = self._average_run_power(scenario.get("current_runs"))
            delta_power = current_power - baseline_power

            change_percent: float | None
            if baseline_power > 0.0:
                change_percent = delta_power / baseline_power * 100.0
            elif current_power == 0.0:
                change_percent = 0.0
            else:
                change_percent = None

            upper_bound = baseline_power * (1.0 + threshold / 100.0)
            lower_bound = baseline_power * (1.0 - threshold / 100.0)
            if change_percent is None or current_power > upper_bound:
                status = "regression"
                regression_count += 1
            elif current_power < lower_bound:
                status = "improvement"
                improvement_count += 1
            else:
                status = "stable"

            results.append(
                {
                    "id": scenario_id,
                    "baseline_power_w": baseline_power,
                    "current_power_w": current_power,
                    "delta_power_w": delta_power,
                    "change_percent": change_percent,
                    "status": status,
                }
            )

        if regression_count:
            overall_status = "regression"
        elif improvement_count:
            overall_status = "improvement"
        else:
            overall_status = "stable"

        return {
            "results": results,
            "regression_count": regression_count,
            "improvement_count": improvement_count,
            "overall_status": overall_status,
        }

    @staticmethod
    def _average_run_power(runs: Any) -> float:
        if not isinstance(runs, list) or not runs:
            raise ApiError(
                422, "invalid_runs", "baseline_runs and current_runs must be non-empty arrays"
            )
        powers: list[float] = []
        for run in runs:
            if not isinstance(run, dict):
                raise ApiError(422, "invalid_runs", "each run must be an object")
            energy = run.get("energy_wh")
            duration = run.get("duration_s")
            if (
                not _is_finite_number(energy)
                or energy < 0
                or not _is_finite_number(duration)
                or duration <= 0
            ):
                raise ApiError(
                    422,
                    "invalid_run_measurement",
                    "energy_wh must be a non-negative finite number and "
                    "duration_s must be a positive finite number",
                )
            powers.append(float(energy) * 3600.0 / float(duration))
        return sum(powers) / len(powers)

    @staticmethod
    def _parse_balance_config(
        config: Any, cell_count: int
    ) -> tuple[float, float, float, float, int]:
        if not isinstance(config, dict):
            raise ApiError(422, "invalid_balance_config", "config must be an object")

        start_delta = config.get("start_delta_v")
        if not _is_finite_number(start_delta) or start_delta <= 0:
            raise ApiError(
                422,
                "invalid_balance_config",
                "start_delta_v must be a positive finite number",
            )

        bleed_current = config.get("bleed_current_a")
        if not _is_finite_number(bleed_current) or bleed_current <= 0:
            raise ApiError(
                422,
                "invalid_balance_config",
                "bleed_current_a must be a positive finite number",
            )

        stop_delta = config.get("stop_delta_v")
        if (
            not _is_finite_number(stop_delta)
            or not 0 <= stop_delta < start_delta
        ):
            raise ApiError(
                422,
                "invalid_balance_config",
                "stop_delta_v must be a finite number within [0, start_delta_v)",
            )

        max_temperature = config.get("max_temperature_c")
        if not _is_finite_number(max_temperature):
            raise ApiError(
                422,
                "invalid_balance_config",
                "max_temperature_c must be a finite number",
            )

        max_channels = config.get("max_channels")
        if (
            not isinstance(max_channels, int)
            or isinstance(max_channels, bool)
            or not 1 <= max_channels <= cell_count
        ):
            raise ApiError(
                422,
                "invalid_balance_config",
                "max_channels must be a positive integer not exceeding the cell count",
            )

        return (
            float(start_delta),
            float(bleed_current),
            float(stop_delta),
            float(max_temperature),
            max_channels,
        )

    @staticmethod
    def _parse_previous_active_ids(raw: Any, ids: list[str]) -> set[str]:
        if not isinstance(raw, list):
            raise ApiError(
                422,
                "invalid_previous_active_ids",
                "previous_active_ids must be an array of cell ids",
            )
        known_ids = set(ids)
        previous_active: set[str] = set()
        for cell_id in raw:
            if not isinstance(cell_id, str) or not cell_id:
                raise ApiError(
                    422,
                    "invalid_previous_active_ids",
                    "previous_active_ids items must be non-empty cell ids",
                )
            if cell_id in previous_active:
                raise ApiError(
                    422,
                    "invalid_previous_active_ids",
                    f"previous cell id {cell_id!r} is duplicated",
                )
            if cell_id not in known_ids:
                raise ApiError(
                    422,
                    "invalid_previous_active_ids",
                    f"previous cell id {cell_id!r} is unknown",
                )
            previous_active.add(cell_id)
        return previous_active

    @staticmethod
    def _parse_charge_config(config: Any, target_soc: float) -> tuple[
        float, float, float, float, float
    ]:
        if not isinstance(config, dict):
            raise ApiError(422, "invalid_charge_config", "config must be an object")

        max_current = config.get("max_current_a")
        if not _is_finite_number(max_current) or max_current <= 0:
            raise ApiError(
                422,
                "invalid_charge_config",
                "max_current_a must be a positive finite number",
            )

        charge_voltage = config.get("charge_voltage_v")
        if not _is_finite_number(charge_voltage) or charge_voltage <= 0:
            raise ApiError(
                422,
                "invalid_charge_config",
                "charge_voltage_v must be a positive finite number",
            )

        coulombic_efficiency = config.get("coulombic_efficiency")
        if (
            not _is_finite_number(coulombic_efficiency)
            or not 0.0 < coulombic_efficiency <= 1.0
        ):
            raise ApiError(
                422,
                "invalid_charge_config",
                "coulombic_efficiency must be a finite number within (0, 1]",
            )

        taper_start_soc = config.get("taper_start_soc")
        if (
            not _is_finite_number(taper_start_soc)
            or not 0 <= taper_start_soc < target_soc
        ):
            raise ApiError(
                422,
                "invalid_charge_config",
                "taper_start_soc must be a finite number within "
                "[0, target_soc)",
            )

        taper_end_current = config.get("taper_end_current_a")
        if (
            not _is_finite_number(taper_end_current)
            or not 0.0 < taper_end_current <= float(max_current)
        ):
            raise ApiError(
                422,
                "invalid_charge_config",
                "taper_end_current_a must be a positive finite number not "
                "exceeding max_current_a",
            )

        return (
            float(max_current),
            float(charge_voltage),
            float(coulombic_efficiency),
            float(taper_start_soc),
            float(taper_end_current),
        )

    @staticmethod
    def _parse_charge_plan_options(payload: dict[str, Any]) -> tuple[float, int]:
        step_duration = payload.get("step_duration_s")
        if not _is_finite_number(step_duration) or step_duration <= 0:
            raise ApiError(
                422,
                "invalid_plan_options",
                "step_duration_s must be a positive finite number",
            )

        max_steps = payload.get("max_steps")
        if (
            not isinstance(max_steps, int)
            or isinstance(max_steps, bool)
            or max_steps < 1
        ):
            raise ApiError(
                422,
                "invalid_plan_options",
                "max_steps must be a positive integer",
            )

        return float(step_duration), max_steps

    @staticmethod
    def _parse_harvester_config(
        config: Any,
    ) -> tuple[float, list[tuple[float, float]]]:
        if not isinstance(config, dict):
            raise ApiError(
                422, "invalid_harvester_config", "harvester must be an object"
            )

        max_input_power = config.get("max_input_power_w")
        if not _is_finite_number(max_input_power) or max_input_power <= 0:
            raise ApiError(
                422,
                "invalid_harvester_config",
                "max_input_power_w must be a positive finite number",
            )

        curve = config.get("efficiency_curve")
        if not isinstance(curve, list) or len(curve) < 2:
            raise ApiError(
                422,
                "invalid_harvester_config",
                "efficiency_curve must contain at least two points",
            )
        points: list[tuple[float, float]] = []
        previous_power: float | None = None
        for point in curve:
            if not isinstance(point, dict):
                raise ApiError(
                    422,
                    "invalid_harvester_config",
                    "each efficiency curve point must be an object",
                )
            input_power = point.get("input_power_w")
            efficiency = point.get("efficiency")
            if not _is_finite_number(input_power) or input_power < 0:
                raise ApiError(
                    422,
                    "invalid_harvester_config",
                    "efficiency curve input_power_w must be a non-negative "
                    "finite number",
                )
            if not _is_finite_number(efficiency) or not 0.0 < efficiency <= 1.0:
                raise ApiError(
                    422,
                    "invalid_harvester_config",
                    "efficiency curve efficiency must be within (0, 1]",
                )
            if previous_power is not None and input_power <= previous_power:
                raise ApiError(
                    422,
                    "invalid_harvester_config",
                    "efficiency curve input_power_w must be strictly increasing",
                )
            points.append((float(input_power), float(efficiency)))
            previous_power = input_power

        return float(max_input_power), points

    @staticmethod
    def _parse_efficiency_curve(curve: Any) -> list[tuple[float, float]]:
        if not isinstance(curve, list) or len(curve) < 2:
            raise ApiError(
                422,
                "invalid_efficiency_curve",
                "efficiency_curve must contain at least two points",
            )
        points: list[tuple[float, float]] = []
        previous_current: float | None = None
        for point in curve:
            if not isinstance(point, dict):
                raise ApiError(
                    422,
                    "invalid_efficiency_curve",
                    "each efficiency curve point must be an object",
                )
            current = point.get("output_current_a")
            efficiency = point.get("efficiency")
            if not _is_finite_number(current) or current < 0:
                raise ApiError(
                    422,
                    "invalid_efficiency_curve",
                    "efficiency curve output_current_a must be a non-negative "
                    "finite number",
                )
            if not _is_finite_number(efficiency) or not 0.0 < efficiency <= 1.0:
                raise ApiError(
                    422,
                    "invalid_efficiency_curve",
                    "efficiency curve efficiency must be within (0, 1]",
                )
            if previous_current is not None and current <= previous_current:
                raise ApiError(
                    422,
                    "invalid_efficiency_curve",
                    "efficiency curve output_current_a must be strictly increasing",
                )
            points.append((float(current), float(efficiency)))
            previous_current = current
        return points

    @staticmethod
    def _parse_dropout_curve(curve: Any) -> list[tuple[float, float]]:
        if not isinstance(curve, list) or len(curve) < 2:
            raise ApiError(
                422,
                "invalid_dropout_curve",
                "dropout_curve must contain at least two points",
            )
        points: list[tuple[float, float]] = []
        previous_current: float | None = None
        for point in curve:
            if not isinstance(point, dict):
                raise ApiError(
                    422,
                    "invalid_dropout_curve",
                    "each dropout curve point must be an object",
                )
            current = point.get("output_current_a")
            dropout = point.get("dropout_voltage_v")
            if not _is_finite_number(current) or current < 0:
                raise ApiError(
                    422,
                    "invalid_dropout_curve",
                    "dropout curve output_current_a must be a non-negative "
                    "finite number",
                )
            if not _is_finite_number(dropout) or dropout < 0:
                raise ApiError(
                    422,
                    "invalid_dropout_curve",
                    "dropout curve dropout_voltage_v must be a non-negative "
                    "finite number",
                )
            if previous_current is not None and current <= previous_current:
                raise ApiError(
                    422,
                    "invalid_dropout_curve",
                    "dropout curve output_current_a must be strictly increasing",
                )
            points.append((float(current), float(dropout)))
            previous_current = current
        return points

    @staticmethod
    def _parse_filter_options(
        payload: dict[str, Any],
    ) -> tuple[int, float, float]:
        median_window = payload.get("median_window", DEFAULT_MEDIAN_WINDOW)
        if (
            not isinstance(median_window, int)
            or isinstance(median_window, bool)
            or not 1 <= median_window <= 11
            or median_window % 2 == 0
        ):
            raise ApiError(
                422,
                "invalid_filter_options",
                "median_window must be an odd integer within [1, 11]",
            )

        smoothing_factor = payload.get("smoothing_factor", DEFAULT_SMOOTHING_FACTOR)
        if not _is_finite_number(smoothing_factor) or not 0.0 < smoothing_factor <= 1.0:
            raise ApiError(
                422,
                "invalid_filter_options",
                "smoothing_factor must be a finite number within (0, 1]",
            )

        reset_gap_s = payload.get("reset_gap_s", DEFAULT_RESET_GAP_S)
        if not _is_finite_number(reset_gap_s) or reset_gap_s <= 0.0:
            raise ApiError(
                422,
                "invalid_filter_options",
                "reset_gap_s must be a positive finite number",
            )

        return median_window, float(smoothing_factor), float(reset_gap_s)

    @staticmethod
    def _parse_aggregate_options(payload: dict[str, Any]) -> tuple[float, float, float]:
        bucket_duration = payload.get("bucket_duration_s")
        if not _is_finite_number(bucket_duration) or bucket_duration <= 0.0:
            raise ApiError(
                422,
                "invalid_options",
                "bucket_duration_s must be a positive finite number",
            )

        max_gap = payload.get("max_gap_s")
        if not _is_finite_number(max_gap) or max_gap <= 0.0:
            raise ApiError(
                422,
                "invalid_options",
                "max_gap_s must be a positive finite number",
            )

        trend_threshold = payload.get(
            "trend_threshold_w_per_hour", DEFAULT_TREND_THRESHOLD_W_PER_HOUR
        )
        if not _is_finite_number(trend_threshold) or trend_threshold < 0.0:
            raise ApiError(
                422,
                "invalid_options",
                "trend_threshold_w_per_hour must be a non-negative finite number",
            )

        return float(bucket_duration), float(max_gap), float(trend_threshold)

    @staticmethod
    def _parse_trend_config(
        config: Any,
    ) -> tuple[int, int, float, float, float]:
        if not isinstance(config, dict):
            raise ApiError(
                422, "invalid_trend_config", "config must be an object"
            )

        baseline_window = config.get("baseline_window")
        recovery_windows = config.get("recovery_windows")
        if (
            not isinstance(baseline_window, int)
            or isinstance(baseline_window, bool)
            or baseline_window < 1
            or not isinstance(recovery_windows, int)
            or isinstance(recovery_windows, bool)
            or recovery_windows < 1
        ):
            raise ApiError(
                422,
                "invalid_trend_config",
                "baseline_window and recovery_windows must be positive integers",
            )

        min_coverage_ratio = config.get("min_coverage_ratio")
        if (
            not _is_finite_number(min_coverage_ratio)
            or not 0.0 <= min_coverage_ratio <= 1.0
        ):
            raise ApiError(
                422,
                "invalid_trend_config",
                "min_coverage_ratio must be a finite number within [0, 1]",
            )

        warning_deviation = config.get("warning_deviation_w")
        critical_deviation = config.get("critical_deviation_w")
        if (
            not _is_finite_number(warning_deviation)
            or not _is_finite_number(critical_deviation)
            or not 0.0 <= warning_deviation < critical_deviation
        ):
            raise ApiError(
                422,
                "invalid_trend_config",
                "thresholds must satisfy 0 <= warning_deviation_w < "
                "critical_deviation_w",
            )

        return (
            baseline_window,
            recovery_windows,
            float(min_coverage_ratio),
            float(warning_deviation),
            float(critical_deviation),
        )

    @staticmethod
    def _fund_levels(
        targets: list[float],
        priorities: list[int],
        ids: list[str],
        allocated: list[float],
        remaining: float,
    ) -> float:
        """Fund loads toward ``targets`` by descending priority level.

        Fully funds each level while the remaining power covers the
        level's unmet need; otherwise splits the remainder across the
        level proportionally to unmet need and returns zero. Members are
        visited in id order so results do not depend on input ordering.
        """
        by_priority: dict[int, list[int]] = {}
        for index, priority in enumerate(priorities):
            by_priority.setdefault(priority, []).append(index)
        for priority in sorted(by_priority, reverse=True):
            if remaining <= 0.0:
                break
            members = [
                index
                for index in sorted(by_priority[priority], key=lambda i: ids[i])
                if targets[index] > allocated[index]
            ]
            if not members:
                continue
            needs = [targets[index] - allocated[index] for index in members]
            level_total = sum(needs)
            if level_total <= remaining:
                for index in members:
                    allocated[index] = targets[index]
                remaining -= level_total
            else:
                for index, need in zip(members, needs):
                    allocated[index] += remaining * need / level_total
                remaining = 0.0
                break
        return remaining

    @staticmethod
    def _parse_fit_options(options: Any) -> float:
        if options is None:
            return DEFAULT_MIN_VOLTAGE_STEP_V
        if not isinstance(options, dict):
            raise ApiError(
                422, "invalid_fit_options", "options must be an object"
            )
        min_voltage_step = options.get(
            "min_voltage_step_v", DEFAULT_MIN_VOLTAGE_STEP_V
        )
        if not _is_finite_number(min_voltage_step) or min_voltage_step <= 0:
            raise ApiError(
                422,
                "invalid_fit_options",
                "min_voltage_step_v must be a positive finite number",
            )
        return float(min_voltage_step)

    @staticmethod
    def _parse_battery_model(model: Any) -> tuple[float, float, float]:
        if not isinstance(model, dict):
            raise ApiError(422, "invalid_battery_model", "model must be an object")
        r0 = model.get("r0_ohm")
        r1 = model.get("r1_ohm")
        c1 = model.get("c1_f")
        if (
            not _is_finite_number(r0)
            or r0 <= 0
            or not _is_finite_number(r1)
            or r1 <= 0
            or not _is_finite_number(c1)
            or c1 <= 0
        ):
            raise ApiError(
                422,
                "invalid_battery_model",
                "r0_ohm, r1_ohm and c1_f must be positive finite numbers",
            )
        return float(r0), float(r1), float(c1)

    @staticmethod
    def _parse_model_ocv_curve(curve: Any) -> list[tuple[float, float]]:
        if not isinstance(curve, list) or len(curve) < 2:
            raise ApiError(
                422, "invalid_ocv_curve", "ocv_curve must contain at least two points"
            )
        points: list[tuple[float, float]] = []
        previous_soc: float | None = None
        previous_voltage: float | None = None
        for point in curve:
            if not isinstance(point, dict):
                raise ApiError(
                    422, "invalid_ocv_curve", "each OCV point must be an object"
                )
            voltage = point.get("voltage_v")
            soc = point.get("soc")
            if not _is_finite_number(voltage) or not _is_finite_number(soc):
                raise ApiError(
                    422,
                    "invalid_ocv_curve",
                    "OCV point voltage_v and soc must be finite numbers",
                )
            if not 0 <= soc <= 1:
                raise ApiError(
                    422, "invalid_ocv_curve", "OCV point soc must be within [0, 1]"
                )
            if previous_soc is not None:
                if soc <= previous_soc:
                    raise ApiError(
                        422,
                        "invalid_ocv_curve",
                        "OCV point soc must be strictly increasing",
                    )
                if voltage <= previous_voltage:
                    raise ApiError(
                        422,
                        "invalid_ocv_curve",
                        "OCV point voltage_v must be strictly increasing",
                    )
            points.append((float(soc), float(voltage)))
            previous_soc = soc
            previous_voltage = voltage
        return points

    @staticmethod
    def _parse_ocv_curve(
        curve: Any,
    ) -> list[tuple[float, float]] | None:
        if curve is None:
            return None
        if not isinstance(curve, list) or len(curve) < 2:
            raise ApiError(
                422, "invalid_ocv_curve", "ocv_curve must contain at least two points"
            )
        points: list[tuple[float, float]] = []
        previous_voltage: float | None = None
        previous_soc: float | None = None
        for point in curve:
            if not isinstance(point, dict):
                raise ApiError(
                    422, "invalid_ocv_curve", "each OCV point must be an object"
                )
            voltage = point.get("voltage_v")
            soc = point.get("soc")
            if not _is_finite_number(voltage) or not _is_finite_number(soc):
                raise ApiError(
                    422,
                    "invalid_ocv_curve",
                    "OCV point voltage_v and soc must be finite numbers",
                )
            if not 0 <= soc <= 1:
                raise ApiError(
                    422, "invalid_ocv_curve", "OCV point soc must be within [0, 1]"
                )
            if previous_voltage is not None:
                if voltage <= previous_voltage:
                    raise ApiError(
                        422,
                        "invalid_ocv_curve",
                        "OCV point voltage_v must be strictly increasing",
                    )
                if soc < previous_soc:
                    raise ApiError(
                        422,
                        "invalid_ocv_curve",
                        "OCV point soc must be non-decreasing",
                    )
            points.append((voltage, soc))
            previous_voltage = voltage
            previous_soc = soc
        return points

    @staticmethod
    def _parse_cycle_life_curve(curve: Any) -> list[tuple[float, float]]:
        if not isinstance(curve, list) or len(curve) < 2:
            raise ApiError(
                422,
                "invalid_cycle_life_curve",
                "cycle_life_curve must contain at least two points",
            )
        points: list[tuple[float, float]] = []
        previous_depth: float | None = None
        previous_cycles: float | None = None
        for point in curve:
            if not isinstance(point, dict):
                raise ApiError(
                    422,
                    "invalid_cycle_life_curve",
                    "each cycle life curve point must be an object",
                )
            depth = point.get("depth")
            cycles_to_eol = point.get("cycles_to_eol")
            if not _is_finite_number(depth) or not 0 < depth <= 1:
                raise ApiError(
                    422,
                    "invalid_cycle_life_curve",
                    "cycle life curve depth must be a finite number within (0, 1]",
                )
            if not _is_finite_number(cycles_to_eol) or cycles_to_eol <= 0:
                raise ApiError(
                    422,
                    "invalid_cycle_life_curve",
                    "cycle life curve cycles_to_eol must be a positive finite "
                    "number",
                )
            if previous_depth is not None:
                if depth <= previous_depth:
                    raise ApiError(
                        422,
                        "invalid_cycle_life_curve",
                        "cycle life curve depth must be strictly increasing",
                    )
                if cycles_to_eol > previous_cycles:
                    raise ApiError(
                        422,
                        "invalid_cycle_life_curve",
                        "cycle life curve cycles_to_eol must be non-increasing",
                    )
            points.append((float(depth), float(cycles_to_eol)))
            previous_depth = depth
            previous_cycles = cycles_to_eol
        return points

    @staticmethod
    def _parse_options(payload: dict[str, Any]) -> tuple[float, float, float]:
        rest_current = payload.get("rest_current_a", DEFAULT_REST_CURRENT_A)
        rest_duration = payload.get("rest_duration_s", DEFAULT_REST_DURATION_S)
        ocv_weight = payload.get("ocv_weight", DEFAULT_OCV_WEIGHT)
        if (
            not _is_finite_number(rest_current)
            or not _is_finite_number(rest_duration)
            or not _is_finite_number(ocv_weight)
        ):
            raise ApiError(
                422,
                "invalid_options",
                "rest_current_a, rest_duration_s and ocv_weight must be finite numbers",
            )
        if rest_current < 0 or rest_duration < 0 or not 0 <= ocv_weight <= 1:
            raise ApiError(
                422,
                "invalid_options",
                "rest_current_a and rest_duration_s must be non-negative and "
                "ocv_weight must be within [0, 1]",
            )
        return rest_current, rest_duration, ocv_weight
