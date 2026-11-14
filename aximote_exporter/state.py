"""Vehicle state polling: API JSON to Prometheus snapshots and history rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .api import AximoteApiClient
from .metrics import VehicleSnapshot

_LOGGER = logging.getLogger(__name__)

MPS_TO_KMH = 3.6

# Snapshot field name -> the API's timestamp key for that reading. The API stamps
# each reading separately and they legitimately differ by hours.
_FIELD_TIMESTAMPS: tuple[tuple[str, str], ...] = (
    ("state", "capturedAt"),
    ("status", "statusCapturedAt"),
    ("battery", "batteryCapturedAt"),
    ("range", "rangeCapturedAt"),
    ("fuel", "fuelCapturedAt"),
    ("outside_temperature", "outsideTemperatureCapturedAt"),
    ("ignition", "ignitionCapturedAt"),
)


@dataclass
class StateSample:
    """One telemetry poll, for the Postgres history table.

    Prometheus holds the same numbers but makes a map hard: two separate gauges
    cannot be joined into a track. A row per poll makes a Grafana geomap a one-line
    query and survives Prometheus retention.

    ``location_captured_at`` is kept separately from ``captured_at`` because the GPS
    fix is frequently much older than the status poll. Without it, a parked car's
    repeated identical fix is indistinguishable from genuine movement, and the map
    query cannot deduplicate.
    """

    vehicle_id: str
    captured_at: datetime | None
    location_captured_at: datetime | None
    latitude: float | None
    longitude: float | None
    accuracy_m: float | None
    speed_kmh: float | None
    bearing_deg: float | None
    fuel_level_pct: float | None
    battery_level_pct: float | None
    range_km: float | None
    odometer_km: float | None
    outside_temperature: float | None
    on_trip: bool | None
    ignition_on: bool | None
    trip_id: str | None


def num(val: Any) -> float | None:
    """Coerce an API value to float, or None if absent or not numeric."""
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def flag(val: Any) -> float | None:
    """Coerce an API boolean to 1.0/0.0, or None if absent."""
    if val is None:
        return None
    return 1.0 if bool(val) else 0.0


def parse_ts(val: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp into an aware datetime, or None.

    The API mixes precisions -- millisecond, microsecond and nanosecond fractions all
    appear in a single state response -- and ``fromisoformat`` rejects more than six
    fractional digits on older Python, so the fraction is truncated first.
    """
    if not isinstance(val, str) or not val:
        return None
    text = val.strip().replace("Z", "+00:00")
    text = _truncate_fraction(text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _LOGGER.debug("Could not parse timestamp %r", val)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _truncate_fraction(text: str) -> str:
    """Clamp a fractional-seconds component to six digits."""
    dot = text.find(".")
    if dot == -1:
        return text
    end = dot + 1
    while end < len(text) and text[end].isdigit():
        end += 1
    digits = text[dot + 1 : end]
    if len(digits) <= 6:
        return text
    return f"{text[:dot]}.{digits[:6]}{text[end:]}"


def _location(state: dict[str, Any]) -> dict[str, Any]:
    loc = state.get("location")
    return loc if isinstance(loc, dict) else {}


def _vehicle_labels(vehicle: dict[str, Any], vehicle_id: str) -> dict[str, str]:
    name = vehicle.get("name")
    return {
        "vehicle_id": vehicle_id,
        "name": str(name) if name else f"vehicle-{vehicle_id[:8]}",
        "make": str(vehicle.get("make") or ""),
        "model": str(vehicle.get("model") or ""),
    }


def _location_captured_at(state: dict[str, Any]) -> datetime | None:
    """The GPS fix time, from either the top level or inside the location object."""
    stamped = parse_ts(state.get("locationCapturedAt"))
    if stamped is not None:
        return stamped
    return parse_ts(_location(state).get("capturedAt"))


def build_snapshot(vehicle: dict[str, Any], state: dict[str, Any]) -> VehicleSnapshot:
    """Flatten one vehicle's profile and state into Prometheus readings."""
    vehicle_id = str(vehicle.get("id", ""))
    loc = _location(state)
    speed_mps = num(loc.get("speedMps"))

    readings: dict[str, float | None] = {
        "fuel": num(state.get("fuelLevelPct")),
        "battery": num(state.get("batteryLevelPct")),
        "range": num(state.get("rangeKm")),
        "odometer": num(state.get("odometerKm")) or num(vehicle.get("odometerKm")),
        "speed": round(speed_mps * MPS_TO_KMH, 3) if speed_mps is not None else None,
        "bearing": num(loc.get("bearingDeg")),
        "latitude": num(loc.get("latitude")),
        "longitude": num(loc.get("longitude")),
        "accuracy": num(loc.get("accuracyM")),
        "outside_temperature": num(state.get("outsideTemperature")),
        "on_trip": flag(state.get("onTrip")),
        "ignition": flag(state.get("ignitionOn")),
        "fuel_capacity": num(vehicle.get("fuelCapacityL")),
        "battery_capacity": num(vehicle.get("batteryCapacityKwh")),
    }

    timestamps: dict[str, float | None] = {}
    for field, key in _FIELD_TIMESTAMPS:
        parsed = parse_ts(state.get(key))
        timestamps[field] = parsed.timestamp() if parsed else None
    located = _location_captured_at(state)
    timestamps["location"] = located.timestamp() if located else None

    return VehicleSnapshot(_vehicle_labels(vehicle, vehicle_id), readings, timestamps)


def build_state_sample(vehicle: dict[str, Any], state: dict[str, Any]) -> StateSample:
    """Flatten one vehicle's state into a history row."""
    loc = _location(state)
    speed_mps = num(loc.get("speedMps"))
    trip_id = state.get("currentTripId")
    on_trip = state.get("onTrip")
    ignition = state.get("ignitionOn")

    return StateSample(
        vehicle_id=str(vehicle.get("id", "")),
        captured_at=parse_ts(state.get("capturedAt")),
        location_captured_at=_location_captured_at(state),
        latitude=num(loc.get("latitude")),
        longitude=num(loc.get("longitude")),
        accuracy_m=num(loc.get("accuracyM")),
        speed_kmh=round(speed_mps * MPS_TO_KMH, 3) if speed_mps is not None else None,
        bearing_deg=num(loc.get("bearingDeg")),
        fuel_level_pct=num(state.get("fuelLevelPct")),
        battery_level_pct=num(state.get("batteryLevelPct")),
        range_km=num(state.get("rangeKm")),
        odometer_km=num(state.get("odometerKm")),
        outside_temperature=num(state.get("outsideTemperature")),
        on_trip=None if on_trip is None else bool(on_trip),
        ignition_on=None if ignition is None else bool(ignition),
        trip_id=str(trip_id) if trip_id else None,
    )


async def fetch_vehicle_states(
    client: AximoteApiClient,
    vehicles: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Fetch state for every vehicle, sequentially.

    Sequential rather than concurrent: a handful of vehicles polled every few
    minutes is nowhere near needing parallelism, and serialising keeps the request
    rate predictable against an API that does return 429.
    """
    states: dict[str, dict[str, Any]] = {}
    for vehicle in vehicles:
        vehicle_id = vehicle.get("id")
        if vehicle_id is None:
            _LOGGER.warning("Skipping vehicle with no id: keys=%s", sorted(vehicle))
            continue
        states[str(vehicle_id)] = await client.async_get_vehicle_state(str(vehicle_id))
    return states
