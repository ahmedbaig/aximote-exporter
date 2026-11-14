"""Prometheus metrics for Aximote vehicle state.

Live state is exposed through a custom collector reading a snapshot the poller
replaces wholesale. That avoids two failure modes a plain ``Gauge.set()`` has:

* a value the API reports as ``null`` would keep its previous number forever,
  silently presenting a stale reading as current -- and for an EV, ``fuelLevelPct``
  is always null, so this is the normal case, not an edge case;
* a vehicle removed from the account would keep emitting its last known series.

Counters that genuinely accumulate (errors, poll totals) stay as normal metrics.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

_LOGGER = logging.getLogger(__name__)

# Labels carried by every per-vehicle series. Kept small and bounded on purpose:
# latitude/longitude are values, never labels, or every poll mints a new series.
# VIN is deliberately absent -- it identifies the car, never changes, and would be
# copied into every sample of every series.
_VEHICLE_LABELS = ("vehicle_id", "name", "make", "model")

# (metric name, help text, snapshot key). A key missing from the snapshot means
# "no reading this cycle" and the series is omitted entirely.
_STATE_METRICS: tuple[tuple[str, str, str], ...] = (
    ("aximote_fuel_level_percent", "Fuel level reported by the vehicle", "fuel"),
    ("aximote_battery_level_percent", "Traction battery level", "battery"),
    ("aximote_range_km", "Estimated remaining range", "range"),
    ("aximote_odometer_km", "Odometer reading", "odometer"),
    ("aximote_speed_kmh", "Instantaneous speed at the last GPS fix", "speed"),
    ("aximote_bearing_degrees", "Heading at the last GPS fix", "bearing"),
    ("aximote_location_latitude", "Latitude of the last GPS fix", "latitude"),
    ("aximote_location_longitude", "Longitude of the last GPS fix", "longitude"),
    (
        "aximote_location_accuracy_meters",
        "Reported accuracy of the last GPS fix",
        "accuracy",
    ),
    (
        "aximote_outside_temperature_celsius",
        "Outside temperature reported by the vehicle",
        "outside_temperature",
    ),
    ("aximote_on_trip", "1 when the vehicle is on a trip", "on_trip"),
    (
        "aximote_ignition_on",
        "1 when ignition is reported on. Latches: check "
        'aximote_field_captured_timestamp_seconds{field="ignition"} before trusting it',
        "ignition",
    ),
    (
        "aximote_fuel_capacity_liters",
        "Tank capacity from the vehicle profile",
        "fuel_capacity",
    ),
    (
        "aximote_battery_capacity_kwh",
        "Battery capacity from the vehicle profile",
        "battery_capacity",
    ),
)

# Per-field capture times. The API timestamps each reading separately and they
# disagree by hours: a current `capturedAt` with a two-hour-old `locationCapturedAt`
# is normal. One overall freshness number would report green while GPS is stale,
# so every field's age is exported and alerted on individually.
_TIMESTAMP_FIELDS = (
    "state",
    "status",
    "location",
    "battery",
    "range",
    "fuel",
    "outside_temperature",
    "ignition",
)

poll_errors = Counter(
    "aximote_api_errors_total",
    "API calls that failed, by error code",
    ["code"],
)
poll_total = Counter(
    "aximote_polls_total",
    "Poll cycles attempted, by loop",
    ["loop"],
)
poll_duration = Histogram(
    "aximote_poll_duration_seconds",
    "Wall-clock duration of a poll cycle, by loop",
    ["loop"],
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)
events_ingested = Counter(
    "aximote_events_ingested_total",
    "Trip and refuel records written to Postgres, by kind",
    ["kind"],
)
state_samples_written = Counter(
    "aximote_state_samples_written_total",
    "Telemetry history rows actually inserted into Postgres. Rises more slowly "
    "than aximote_polls_total because a poll of a car that has not reported since "
    "the last poll carries the same capture time and is deduplicated away",
)


class VehicleSnapshot:
    """Flat, already-coerced readings for one vehicle."""

    __slots__ = ("field_timestamps", "labels", "readings")

    def __init__(
        self,
        labels: dict[str, str],
        readings: dict[str, float | None],
        field_timestamps: dict[str, float | None],
    ) -> None:
        self.labels = labels
        self.readings = readings
        self.field_timestamps = field_timestamps


class AximoteCollector(Collector):
    """Serves the latest snapshot the state poller published."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: list[VehicleSnapshot] = []
        self._api_up: float = 0.0
        self._last_success_ts: float | None = None

    def publish(
        self,
        snapshots: Iterable[VehicleSnapshot],
        *,
        api_up: bool,
        last_success_ts: float | None,
    ) -> None:
        """Replace the served snapshot. Called once per state poll."""
        with self._lock:
            self._snapshots = list(snapshots)
            self._api_up = 1.0 if api_up else 0.0
            if last_success_ts is not None:
                self._last_success_ts = last_success_ts

    def mark_failure(self) -> None:
        """Flag the API as down without discarding the last known readings.

        Readings are kept so a transient failure does not blank every dashboard
        panel; ``aximote_api_up`` and the per-field capture times are what tell you
        the data has gone stale.
        """
        with self._lock:
            self._api_up = 0.0

    def collect(self):
        with self._lock:
            snapshots = list(self._snapshots)
            api_up_value = self._api_up
            last_success = self._last_success_ts

        yield GaugeMetricFamily(
            "aximote_api_up",
            "1 when the last Aximote API poll succeeded",
            value=api_up_value,
        )
        if last_success is not None:
            yield GaugeMetricFamily(
                "aximote_last_success_timestamp_seconds",
                "Unix time of the last successful poll of the Aximote API",
                value=last_success,
            )
        yield GaugeMetricFamily(
            "aximote_vehicles",
            "Vehicles returned by the API for this account",
            value=float(len(snapshots)),
        )

        for name, docs, key in _STATE_METRICS:
            family = GaugeMetricFamily(name, docs, labels=list(_VEHICLE_LABELS))
            emitted = False
            for snap in snapshots:
                value = snap.readings.get(key)
                if value is None:
                    continue
                family.add_metric(_label_values(snap), value)
                emitted = True
            if emitted:
                yield family

        ages = GaugeMetricFamily(
            "aximote_field_captured_timestamp_seconds",
            "Unix time each individual reading was captured by the in-car agent",
            labels=[*_VEHICLE_LABELS, "field"],
        )
        emitted = False
        for snap in snapshots:
            for field in _TIMESTAMP_FIELDS:
                value = snap.field_timestamps.get(field)
                if value is None:
                    continue
                ages.add_metric([*_label_values(snap), field], value)
                emitted = True
        if emitted:
            yield ages


def _label_values(snap: VehicleSnapshot) -> list[str]:
    return [snap.labels.get(label, "") for label in _VEHICLE_LABELS]


def register_collector() -> AximoteCollector:
    """Create the collector and attach it to the default registry."""
    collector = AximoteCollector()
    REGISTRY.register(collector)
    return collector
