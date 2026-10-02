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
    """Return the median of ``values``; even counts average the middle pair."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
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

    def filter_telemetry(self, payload: Any) -> dict[str, Any]:
        """Filter spiky, unevenly spaced telemetry into a stable sequence.

        The two channels are filtered independently but share segment
        boundaries: a gap strictly larger than ``reset_gap_s`` starts a
        fresh segment. Within a segment each reading is replaced by the
        median of the current raw value and up to ``median_window - 1``
        earlier raw values; the segment's first reading adopts that
        median directly and later readings blend it with the previous
        filtered value using ``smoothing_factor``.
        """
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_json", "request body must be a JSON object")

        samples = payload.get("samples")
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "invalid_samples", "samples must be a non-empty array")

        timestamps: list[Any] = []
        timestamp_values: list[float] = []
        currents: list[float] = []
        voltages: list[float] = []
        previous_timestamp: float | None = None
        for sample in samples:
            if not isinstance(sample, dict):
                raise ApiError(422, "invalid_samples", "each sample must be an object")
            timestamp = sample.get("timestamp_s")
            if not _is_finite_number(timestamp):
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be a finite number"
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ApiError(
                    422, "invalid_timestamp", "timestamp_s must be strictly increasing"
                )
            current = sample.get("current_a")
            voltage = sample.get("voltage_v")
            if not _is_finite_number(current) or not _is_finite_number(voltage):
                raise ApiError(
                    422,
                    "invalid_measurement",
                    "current_a and voltage_v must be finite numbers",
                )
            timestamps.append(timestamp)
            timestamp_values.append(float(timestamp))
            currents.append(float(current))
            voltages.append(float(voltage))
            previous_timestamp = float(timestamp)

        median_window, smoothing_factor, reset_gap_s = self._parse_filter_options(
            payload
        )

        filtered: list[dict[str, Any]] = []
        segment_count = 0
        current_history: list[float] = []
        voltage_history: list[float] = []
        previous_current: float | None = None
        previous_voltage: float | None = None

        for index in range(len(samples)):
            timestamp = timestamps[index]
            segment_start = index == 0 or (
                timestamp_values[index] - timestamp_values[index - 1] > reset_gap_s
            )
            if segment_start:
                segment_count += 1
                current_history = []
                voltage_history = []
                previous_current = None
                previous_voltage = None

            current_history.append(currents[index])
            voltage_history.append(voltages[index])
            del current_history[:-median_window]
            del voltage_history[:-median_window]
            median_current = _median(current_history)
            median_voltage = _median(voltage_history)

            if previous_current is None:
                filtered_current = median_current
                filtered_voltage = median_voltage
            else:
                filtered_current = (
                    smoothing_factor * median_current
                    + (1.0 - smoothing_factor) * previous_current
                )
                filtered_voltage = (
                    smoothing_factor * median_voltage
                    + (1.0 - smoothing_factor) * previous_voltage
                )

            filtered.append(
                {
                    "timestamp_s": timestamp,
                    "filtered_current_a": filtered_current,
                    "filtered_voltage_v": filtered_voltage,
                    "segment_start": segment_start,
                }
            )
            previous_current = filtered_current
            previous_voltage = filtered_voltage

        return {
            "samples": filtered,
            "segment_count": segment_count,
            "sample_count": len(filtered),
        }

    @staticmethod
    def _parse_filter_options(
        payload: dict[str, Any],
    ) -> tuple[int, float, float]:
        median_window = payload.get("median_window", DEFAULT_MEDIAN_WINDOW)
        smoothing_factor = payload.get("smoothing_factor", DEFAULT_SMOOTHING_FACTOR)
        reset_gap_s = payload.get("reset_gap_s", DEFAULT_RESET_GAP_S)
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
        if not _is_finite_number(smoothing_factor) or not 0 < smoothing_factor <= 1:
            raise ApiError(
                422,
                "invalid_filter_options",
                "smoothing_factor must be a finite number within (0, 1]",
            )
        if not _is_finite_number(reset_gap_s) or reset_gap_s <= 0:
            raise ApiError(
                422,
                "invalid_filter_options",
                "reset_gap_s must be a positive finite number",
            )
        return median_window, float(smoothing_factor), float(reset_gap_s)

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
