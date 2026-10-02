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

    def allocate_power_budget(self, payload: Any) -> dict[str, Any]:
        """Allocate an available power budget across prioritized loads.

        Minimum power is satisfied first, highest priority tier first; a
        tier whose minimums exceed the remaining budget splits it
        proportionally to each load's minimum and lower tiers get nothing.
        Leftover power then tops up unmet demand in the same priority
        order, split proportionally to unmet demand within a tier.
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
        reserve = payload.get("reserve_power_w", 0)
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

        seen_ids: set[str] = set()
        for load in loads:
            load_id = load.get("id")
            if not isinstance(load_id, str) or not load_id:
                raise ApiError(
                    422, "invalid_load_id", "each load id must be a non-empty string"
                )
            if load_id in seen_ids:
                raise ApiError(422, "invalid_load_id", "load ids must be unique")
            seen_ids.add(load_id)

        for load in loads:
            demand = load.get("demand_power_w")
            minimum = load.get("min_power_w")
            if (
                not _is_finite_number(demand)
                or demand < 0
                or not _is_finite_number(minimum)
                or minimum < 0
                or minimum > demand
            ):
                raise ApiError(
                    422,
                    "invalid_load_power",
                    "demand_power_w and min_power_w must be non-negative finite "
                    "numbers with min_power_w not exceeding demand_power_w",
                )

        for load in loads:
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

        parsed = [
            (
                load["id"],
                float(load["demand_power_w"]),
                float(load["min_power_w"]),
                load["priority"],
            )
            for load in loads
        ]
        priorities = sorted({entry[3] for entry in parsed}, reverse=True)

        allocatable = float(available) - float(reserve)
        remaining = allocatable
        allocated = [0.0] * len(parsed)

        # Phase 1: satisfy minimum power, highest priority tier first.
        for priority in priorities:
            indices = [i for i, entry in enumerate(parsed) if entry[3] == priority]
            total_min = sum(parsed[i][2] for i in indices)
            if total_min <= remaining:
                for i in indices:
                    allocated[i] = parsed[i][2]
                remaining -= total_min
            else:
                if total_min > 0:
                    for i in indices:
                        allocated[i] = remaining * parsed[i][2] / total_min
                remaining = 0.0

        # Phase 2: top up unmet demand with the leftover power.
        for priority in priorities:
            indices = [i for i, entry in enumerate(parsed) if entry[3] == priority]
            gaps = [parsed[i][1] - allocated[i] for i in indices]
            total_gap = sum(gaps)
            if total_gap <= 0:
                continue
            if total_gap <= remaining:
                for i in indices:
                    allocated[i] = parsed[i][1]
                remaining -= total_gap
            else:
                for i, gap in zip(indices, gaps):
                    allocated[i] += remaining * gap / total_gap
                remaining = 0.0

        allocations: list[dict[str, Any]] = []
        total_allocated = 0.0
        all_met = True
        for i, (load_id, demand, _minimum, _priority) in enumerate(parsed):
            power = allocated[i]
            if power < 0.0:
                power = 0.0
            elif power > demand:
                power = demand
            shortfall = demand - power
            if shortfall < 0.0:
                shortfall = 0.0
            if power >= demand:
                state = "powered"
            elif power <= 0.0:
                state = "shed"
            else:
                state = "limited"
            if shortfall > 0.0:
                all_met = False
            total_allocated += power
            allocations.append(
                {
                    "id": load_id,
                    "allocated_power_w": power,
                    "shortfall_power_w": shortfall,
                    "state": state,
                }
            )

        unallocated = allocatable - total_allocated
        if unallocated < 0.0:
            unallocated = 0.0

        return {
            "status": "satisfied" if all_met else "constrained",
            "allocated_power_w": total_allocated,
            "unallocated_power_w": unallocated,
            "allocations": allocations,
        }

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
