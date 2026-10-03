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

    def dispatch_parallel(self, payload: Any) -> dict[str, Any]:
        """Dispatch discharge current across parallel battery packs.

        All packs start connected. A pack is isolated for the current
        sample when its ``fault`` flag is set or its voltage differs from
        the bus voltage by more than ``max_bus_voltage_delta_v``; an
        isolated pack reconnects at the last of ``recovery_samples``
        consecutive safe samples, and any unsafe sample resets that
        streak. The requested bus current is shared equally among
        connected packs, capping each at its own limit and redistributing
        the remainder equally among the rest.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        max_delta, recovery_samples = self._parse_parallel_config(
            payload.get("config")
        )

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "invalid_samples", "samples must be a non-empty array")
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
                    422, "invalid_samples", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422,
                    "invalid_samples",
                    "timestamp_s must be strictly increasing",
                )
            previous_timestamp = timestamp

        requested_currents: list[float] = []
        bus_voltages: list[float] = []
        for sample in samples:
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
            requested_currents.append(float(requested))
            bus_voltages.append(float(bus_voltage))

        per_sample_packs: list[list[tuple[str, float, float, bool]]] = []
        reference_ids: set[str] | None = None
        for sample in samples:
            packs = sample.get("packs")
            if not isinstance(packs, list) or not packs:
                raise ApiError(
                    422, "invalid_packs", "packs must be a non-empty array"
                )
            parsed: list[tuple[str, float, float, bool]] = []
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
                        "max_discharge_current_a must be a non-negative finite "
                        "number",
                    )
                fault = pack.get("fault")
                if not isinstance(fault, bool):
                    raise ApiError(
                        422, "invalid_packs", "fault must be a boolean"
                    )
                parsed.append(
                    (pack_id, float(voltage), float(max_current), fault)
                )
            if reference_ids is None:
                reference_ids = seen_ids
            elif seen_ids != reference_ids:
                raise ApiError(
                    422,
                    "invalid_packs",
                    "each sample must carry the same set of pack ids",
                )
            per_sample_packs.append(parsed)

        connected = {pack_id: True for pack_id in reference_ids}
        safe_streak = {pack_id: 0 for pack_id in reference_ids}

        decisions: list[dict[str, Any]] = []
        for index in range(len(samples)):
            parsed = per_sample_packs[index]
            bus_voltage = bus_voltages[index]
            for pack_id, voltage, _max_current, fault in parsed:
                unsafe = fault or abs(voltage - bus_voltage) > max_delta
                if connected[pack_id]:
                    if unsafe:
                        connected[pack_id] = False
                        safe_streak[pack_id] = 0
                elif unsafe:
                    safe_streak[pack_id] = 0
                else:
                    safe_streak[pack_id] += 1
                    if safe_streak[pack_id] >= recovery_samples:
                        connected[pack_id] = True
                        safe_streak[pack_id] = 0

            active_indexes = [
                i for i, pack in enumerate(parsed) if connected[pack[0]]
            ]
            caps = [parsed[i][2] for i in active_indexes]
            shares, satisfied = self._share_bus_current(
                requested_currents[index], caps
            )
            allocated_by_index = dict(zip(active_indexes, shares))

            allocated_total = sum(shares)
            if satisfied:
                allocated_total = requested_currents[index]
            unmet = requested_currents[index] - allocated_total
            if unmet < 0.0:
                unmet = 0.0
                allocated_total = requested_currents[index]

            pack_decisions: list[dict[str, Any]] = []
            for i, pack in enumerate(parsed):
                pack_decisions.append(
                    {
                        "id": pack[0],
                        "allocated_current_a": allocated_by_index.get(i, 0.0),
                        "state": "connected" if connected[pack[0]] else "isolated",
                    }
                )

            if unmet == 0.0:
                status = "satisfied"
            elif requested_currents[index] > 0.0 and not active_indexes:
                status = "no_available_pack"
            else:
                status = "constrained"

            decisions.append(
                {
                    "timestamp_s": samples[index]["timestamp_s"],
                    "pack_decisions": pack_decisions,
                    "allocated_bus_current_a": allocated_total,
                    "unmet_bus_current_a": unmet,
                    "status": status,
                }
            )

        return {"decisions": decisions}

    @staticmethod
    def _parse_parallel_config(config: Any) -> tuple[float, int]:
        if not isinstance(config, dict):
            raise ApiError(
                422, "invalid_parallel_config", "config must be an object"
            )
        max_delta = config.get("max_bus_voltage_delta_v")
        if not _is_finite_number(max_delta) or max_delta < 0:
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
        return float(max_delta), recovery_samples

    @staticmethod
    def _share_bus_current(
        request: float, caps: list[float]
    ) -> tuple[list[float], bool]:
        """Split ``request`` equally across packs, capping at each limit.

        Packs whose equal share would exceed their cap are fixed at the
        cap and the remainder is shared equally among the rest, repeating
        until the request is met or every pack is capped. Returns the
        per-pack allocation and whether the request was fully met.
        """
        allocation = [0.0] * len(caps)
        remaining = request
        active = list(range(len(caps)))
        satisfied = False
        while active and remaining > 0.0:
            share = remaining / len(active)
            capped = [i for i in active if caps[i] <= share]
            if not capped:
                for i in active:
                    allocation[i] = share
                satisfied = True
                remaining = 0.0
            else:
                capped_set = set(capped)
                for i in capped:
                    allocation[i] = caps[i]
                    remaining -= caps[i]
                active = [i for i in active if i not in capped_set]
        return allocation, satisfied

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
