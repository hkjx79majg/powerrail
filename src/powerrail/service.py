"""Core service surface for PowerRail.

Health reporting shares the module with the battery SOC estimation added on
top of the frozen baseline.
"""

from __future__ import annotations

import math
from typing import Any

from . import __version__


class SocError(Exception):
    """A client-side SOC estimation failure carrying a stable error code."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    def payload(self) -> dict[str, str]:
        return {"error": {"code": self.code, "message": str(self)}}


def _finite_number(value: Any) -> bool:
    # bool is a subclass of int; reject it explicitly.
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _require_object(mapping: Any, key: str, code: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise SocError(code, 422, f"missing field: {key}")
    return mapping[key]


def _interpolate_ocv_soc(voltage: float, curve: list[dict[str, float]]) -> float:
    """Map voltage to SOC by linear interpolation over the OCV curve."""
    if voltage <= curve[0]["voltage_v"]:
        return curve[0]["soc"]
    if voltage >= curve[-1]["voltage_v"]:
        return curve[-1]["soc"]
    for left, right in zip(curve, curve[1:]):
        if voltage <= right["voltage_v"]:
            v0, v1 = left["voltage_v"], right["voltage_v"]
            ratio = (voltage - v0) / (v1 - v0)
            return left["soc"] + ratio * (right["soc"] - left["soc"])
    return curve[-1]["soc"]  # unreachable: finite values fall within endpoint checks


class Service:
    """Health reporting and battery SOC estimation."""

    name = "powerrail"
    version = __version__

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    def estimate_soc(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise SocError("invalid_json", 400, "request body must be a JSON object")

        capacity_ah = _require_object(request, "capacity_ah", "invalid_capacity")
        if not _finite_number(capacity_ah) or capacity_ah <= 0:
            raise SocError("invalid_capacity", 422, "capacity_ah must be a positive finite number")

        initial_soc = _require_object(request, "initial_soc", "invalid_initial_soc")
        if not _finite_number(initial_soc) or not (0.0 <= initial_soc <= 1.0):
            raise SocError("invalid_initial_soc", 422, "initial_soc must be a finite number in [0, 1]")

        samples = _require_object(request, "samples", "invalid_samples")
        if not isinstance(samples, list) or not samples:
            raise SocError("invalid_samples", 422, "samples must be a non-empty array")

        rows: list[tuple[Any, float, float, float]] = []
        prev_timestamp: float | None = None
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict) or "timestamp_s" not in sample:
                raise SocError("invalid_timestamp", 422, f"samples[{index}].timestamp_s is required")
            timestamp = sample["timestamp_s"]
            if not _finite_number(timestamp):
                raise SocError("invalid_timestamp", 422, f"samples[{index}].timestamp_s must be finite")
            if prev_timestamp is not None and not timestamp > prev_timestamp:
                raise SocError("invalid_timestamp", 422, "timestamp_s values must be strictly increasing")

            current = sample.get("current_a")
            voltage = sample.get("voltage_v")
            if "current_a" not in sample or not _finite_number(current):
                raise SocError("invalid_measurement", 422, f"samples[{index}].current_a must be a finite number")
            if "voltage_v" not in sample or not _finite_number(voltage):
                raise SocError("invalid_measurement", 422, f"samples[{index}].voltage_v must be a finite number")

            rows.append((timestamp, float(timestamp), float(current), float(voltage)))
            prev_timestamp = float(timestamp)

        rest_current_a, rest_duration_s, ocv_weight = self._parse_options(request)
        curve = self._parse_ocv_curve(request.get("ocv_curve"))

        estimates: list[dict[str, Any]] = []
        soc = float(initial_soc)
        rest_time = 0.0
        capacity_as = float(capacity_ah) * 3600.0

        for index, (timestamp_raw, timestamp, current, voltage) in enumerate(rows):
            source = "coulomb"
            if index == 0:
                # The first estimate adopts initial_soc verbatim.
                estimates.append({"timestamp_s": timestamp_raw, "soc": initial_soc, "source": source})
                continue
            prev_current, prev_timestamp = rows[index - 1][2], rows[index - 1][1]
            current_avg = (prev_current + current) / 2.0
            delta_time = timestamp - prev_timestamp
            soc = min(1.0, max(0.0, soc - current_avg * delta_time / capacity_as))

            if curve is not None:
                if abs(prev_current) <= rest_current_a and abs(current) <= rest_current_a:
                    rest_time += delta_time
                else:
                    rest_time = 0.0
                if rest_time >= rest_duration_s:
                    ocv_soc = _interpolate_ocv_soc(voltage, curve)
                    soc = (1.0 - ocv_weight) * soc + ocv_weight * ocv_soc
                    source = "ocv_corrected"

            estimates.append({"timestamp_s": timestamp_raw, "soc": soc, "source": source})

        return {"estimates": estimates, "final_soc": estimates[-1]["soc"]}

    @staticmethod
    def _parse_options(request: dict[str, Any]) -> tuple[float, float, float]:
        defaults = {"rest_current_a": 0.05, "rest_duration_s": 300.0, "ocv_weight": 0.2}
        values: dict[str, float] = {}
        for key, default in defaults.items():
            value = request.get(key, default)
            if not _finite_number(value):
                raise SocError("invalid_options", 422, f"{key} must be a finite number")
            values[key] = float(value)
        if values["rest_current_a"] < 0.0:
            raise SocError("invalid_options", 422, "rest_current_a must not be negative")
        if values["rest_duration_s"] < 0.0:
            raise SocError("invalid_options", 422, "rest_duration_s must not be negative")
        if not (0.0 <= values["ocv_weight"] <= 1.0):
            raise SocError("invalid_options", 422, "ocv_weight must be in [0, 1]")
        return values["rest_current_a"], values["rest_duration_s"], values["ocv_weight"]

    @staticmethod
    def _parse_ocv_curve(raw: Any) -> list[dict[str, float]] | None:
        if raw is None:
            return None
        if not isinstance(raw, list) or len(raw) < 2:
            raise SocError("invalid_ocv_curve", 422, "ocv_curve must contain at least two points")
        curve: list[dict[str, float]] = []
        for index, point in enumerate(raw):
            if not isinstance(point, dict):
                raise SocError("invalid_ocv_curve", 422, f"ocv_curve[{index}] must be an object")
            voltage = point.get("voltage_v")
            soc = point.get("soc")
            if "voltage_v" not in point or not _finite_number(voltage):
                raise SocError("invalid_ocv_curve", 422, f"ocv_curve[{index}].voltage_v must be finite")
            if "soc" not in point or not _finite_number(soc):
                raise SocError("invalid_ocv_curve", 422, f"ocv_curve[{index}].soc must be finite")
            if not 0.0 <= float(soc) <= 1.0:
                raise SocError("invalid_ocv_curve", 422, f"ocv_curve[{index}].soc must be in [0, 1]")
            curve.append({"voltage_v": float(voltage), "soc": float(soc)})
        for index, (left, right) in enumerate(zip(curve, curve[1:])):
            if not right["voltage_v"] > left["voltage_v"]:
                raise SocError("invalid_ocv_curve", 422, "ocv_curve voltage_v must be strictly increasing")
            if right["soc"] < left["soc"]:
                raise SocError("invalid_ocv_curve", 422, "ocv_curve soc must be non-decreasing")
        return curve
