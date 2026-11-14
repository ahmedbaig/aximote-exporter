"""Postgres persistence for vehicles, trips, refuels and state samples."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import asyncpg

from .state import StateSample, num, parse_ts

_LOGGER = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# 000_bootstrap.sql is deliberately excluded: it creates the role and database and
# needs superuser, so it is run once by hand. Everything from 001 onwards is
# idempotent and owned by the service's own role.
#
# Excluded by prefix rather than by glob -- a character-class pattern over the
# leading digits is easy to get wrong, and did: 0[1-9]*.sql matches nothing here,
# because every filename's second character is the 0 of 001, 002, ...
#
# Dotfiles are skipped too. A tar built on macOS carries AppleDouble sidecars
# named ._001_schema.sql, which match *.sql, are 163 bytes of binary, and fail
# decoding before any statement runs.
SQL_FILE_GLOB = "*.sql"
BOOTSTRAP_PREFIX = "000"


def first(payload: dict[str, Any], *keys: str) -> Any:
    """Return the value of the first key present and non-null, or None."""
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def nested(payload: dict[str, Any], container: str, key: str) -> Any:
    """Read a key out of a nested object, tolerating the object being absent."""
    obj = payload.get(container)
    return obj.get(key) if isinstance(obj, dict) else None


def record_id(payload: dict[str, Any], *extra: str) -> str | None:
    """The API's own id for a record, as text."""
    value = first(payload, "id", *extra)
    return str(value) if value is not None else None


class Store:
    """Thin asyncpg wrapper. All writes are idempotent upserts."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> Store:
        """Open a pool and apply the schema."""
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        store = cls(pool)
        await store.apply_schema()
        return store

    async def close(self) -> None:
        await self._pool.close()

    async def apply_schema(self) -> None:
        """Run every idempotent schema file, in filename order."""
        files = [
            path
            for path in sorted(SQL_DIR.glob(SQL_FILE_GLOB))
            if not path.name.startswith(BOOTSTRAP_PREFIX)
            and not path.name.startswith(".")
        ]
        if not files:
            raise FileNotFoundError(f"No schema files found in {SQL_DIR}")
        async with self._pool.acquire() as conn:
            for path in files:
                await conn.execute(path.read_text(encoding="utf-8"))
                _LOGGER.info("Applied %s", path.name)

    async def upsert_vehicle(self, vehicle: dict[str, Any]) -> None:
        """Insert or refresh one vehicle dimension row."""
        vehicle_id = record_id(vehicle, "vehicleId")
        if vehicle_id is None:
            return

        fuel_type = vehicle.get("fuelType")
        if isinstance(fuel_type, list):
            fuel_type = ", ".join(str(item) for item in fuel_type)
        elif fuel_type is not None:
            fuel_type = str(fuel_type)

        await self._pool.execute(
            """
            INSERT INTO aximote.vehicle (
                vehicle_id, name, make, model, year, vin, fuel_type,
                fuel_capacity_l, battery_capacity_kwh, odometer_km, updated_at, raw
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (vehicle_id) DO UPDATE SET
                name = EXCLUDED.name,
                make = EXCLUDED.make,
                model = EXCLUDED.model,
                year = EXCLUDED.year,
                vin = EXCLUDED.vin,
                fuel_type = EXCLUDED.fuel_type,
                fuel_capacity_l = EXCLUDED.fuel_capacity_l,
                battery_capacity_kwh = EXCLUDED.battery_capacity_kwh,
                odometer_km = EXCLUDED.odometer_km,
                updated_at = EXCLUDED.updated_at,
                raw = EXCLUDED.raw,
                last_seen_at = now()
            """,
            vehicle_id,
            _text(vehicle.get("name")),
            _text(vehicle.get("make")),
            _text(vehicle.get("model")),
            _int(vehicle.get("year")),
            _text(vehicle.get("vin")),
            fuel_type,
            num(vehicle.get("fuelCapacityL")),
            num(vehicle.get("batteryCapacityKwh")),
            num(vehicle.get("odometerKm")),
            parse_ts(vehicle.get("updatedAt")),
            json.dumps(vehicle),
        )

    async def insert_state_samples(self, samples: list[StateSample]) -> int:
        """Append telemetry history rows, ignoring polls already stored.

        Returns the number of rows actually inserted, which is usually fewer than
        the number offered and often zero. ``capturedAt`` advances only when the
        in-car agent reports, not when we poll: four consecutive polls of a parked
        car carry one identical capture time, and three of them are dropped by the
        primary key. The row rate therefore tracks how often the car reports, not
        the poll interval.

        A capture time is required -- without it there is no key at all, and every
        poll of an idle car would append a duplicate reading.
        """
        rows = [
            (
                s.vehicle_id,
                s.captured_at,
                s.location_captured_at,
                s.latitude,
                s.longitude,
                s.accuracy_m,
                s.speed_kmh,
                s.bearing_deg,
                s.fuel_level_pct,
                s.battery_level_pct,
                s.range_km,
                s.odometer_km,
                s.outside_temperature,
                s.on_trip,
                s.ignition_on,
                s.trip_id,
            )
            for s in samples
            if s.captured_at is not None and s.vehicle_id
        ]
        if not rows:
            return 0

        # Inserted one at a time with RETURNING rather than via executemany: only
        # RETURNING distinguishes an insert from a conflict that was skipped, and
        # the batch is one row per vehicle per poll, so there is nothing to batch.
        inserted = 0
        async with self._pool.acquire() as conn:
            for row in rows:
                result = await conn.fetchval(
                    """
                    INSERT INTO aximote.state_sample (
                        vehicle_id, captured_at, location_captured_at, latitude,
                        longitude, accuracy_m, speed_kmh, bearing_deg,
                        fuel_level_pct, battery_level_pct, range_km, odometer_km,
                        outside_temperature, on_trip, ignition_on, trip_id
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                    ON CONFLICT (vehicle_id, captured_at) DO NOTHING
                    RETURNING 1
                    """,
                    *row,
                )
                if result is not None:
                    inserted += 1
        return inserted

    async def upsert_trip(self, trip: dict[str, Any]) -> str | None:
        """Insert or update one trip. Returns its id, or None if unidentifiable."""
        trip_id = record_id(trip, "tripId")
        vehicle_id = first(trip, "vehicleId", "vehicle_id")
        if trip_id is None or vehicle_id is None:
            _LOGGER.warning(
                "Skipping trip with no id or vehicleId: keys=%s", sorted(trip)
            )
            return None

        tags = trip.get("tags")
        tag_list = [str(tag) for tag in tags] if isinstance(tags, list) else None

        await self._pool.execute(
            """
            INSERT INTO aximote.trip (
                trip_id, vehicle_id, start_time, end_time, duration_sec,
                distance_km, avg_speed_kmh, fuel_consumed_l, energy_consumed_kwh,
                co2_kg, eco_score, speed_score, consistency_score,
                outside_temperature, start_latitude, start_longitude,
                start_country, start_locality, end_latitude, end_longitude,
                end_country, end_locality, tags, raw
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                      $17,$18,$19,$20,$21,$22,$23,$24)
            ON CONFLICT (trip_id) DO UPDATE SET
                vehicle_id = EXCLUDED.vehicle_id,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                duration_sec = EXCLUDED.duration_sec,
                distance_km = EXCLUDED.distance_km,
                avg_speed_kmh = EXCLUDED.avg_speed_kmh,
                fuel_consumed_l = EXCLUDED.fuel_consumed_l,
                energy_consumed_kwh = EXCLUDED.energy_consumed_kwh,
                co2_kg = EXCLUDED.co2_kg,
                eco_score = EXCLUDED.eco_score,
                speed_score = EXCLUDED.speed_score,
                consistency_score = EXCLUDED.consistency_score,
                outside_temperature = EXCLUDED.outside_temperature,
                start_latitude = EXCLUDED.start_latitude,
                start_longitude = EXCLUDED.start_longitude,
                start_country = EXCLUDED.start_country,
                start_locality = EXCLUDED.start_locality,
                end_latitude = EXCLUDED.end_latitude,
                end_longitude = EXCLUDED.end_longitude,
                end_country = EXCLUDED.end_country,
                end_locality = EXCLUDED.end_locality,
                tags = EXCLUDED.tags,
                raw = EXCLUDED.raw,
                updated_at = now()
            """,
            trip_id,
            str(vehicle_id),
            parse_ts(first(trip, "startTime", "startedAt")),
            parse_ts(first(trip, "endTime", "endedAt")),
            _int(first(trip, "durationSec", "durationSeconds")),
            num(trip.get("distanceKm")),
            num(trip.get("avgSpeedKmh")),
            num(trip.get("fuelConsumedL")),
            num(trip.get("energyConsumedKwh")),
            num(trip.get("co2Kg")),
            num(trip.get("ecoScore")),
            num(trip.get("speedScore")),
            num(trip.get("consistencyScore")),
            num(trip.get("outsideTemperature")),
            num(nested(trip, "startLocation", "latitude")),
            num(nested(trip, "startLocation", "longitude")),
            _text(nested(trip, "startLocation", "country")),
            _text(nested(trip, "startLocation", "locality")),
            num(nested(trip, "endLocation", "latitude")),
            num(nested(trip, "endLocation", "longitude")),
            _text(nested(trip, "endLocation", "country")),
            _text(nested(trip, "endLocation", "locality")),
            tag_list,
            json.dumps(trip),
        )
        return trip_id

    async def upsert_refuel(self, refuel: dict[str, Any]) -> str | None:
        """Insert or update one refuel/charge. Returns its id, or None."""
        refuel_id = record_id(refuel, "refuelId")
        vehicle_id = first(refuel, "vehicleId", "vehicle_id")
        if refuel_id is None or vehicle_id is None:
            _LOGGER.warning(
                "Skipping refuel with no id or vehicleId: keys=%s",
                sorted(refuel),
            )
            return None

        kind = refuel.get("type")
        await self._pool.execute(
            """
            INSERT INTO aximote.refuel (
                refuel_id, vehicle_id, trip_id, kind, is_active,
                start_time, end_time, duration_sec,
                resolved_start_time, resolved_end_time, resolved_duration_sec,
                window_source, start_fuel_level_pct, end_fuel_level_pct,
                fuel_amount_l, start_energy_kwh, end_energy_kwh,
                energy_charged_kwh, avg_charging_kw, range_at_start_km,
                range_at_end_km, cost_amount, cost_currency,
                outside_temperature, odometer_km, latitude, longitude, raw
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                      $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28)
            ON CONFLICT (refuel_id) DO UPDATE SET
                vehicle_id = EXCLUDED.vehicle_id,
                trip_id = EXCLUDED.trip_id,
                kind = EXCLUDED.kind,
                is_active = EXCLUDED.is_active,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                duration_sec = EXCLUDED.duration_sec,
                resolved_start_time = EXCLUDED.resolved_start_time,
                resolved_end_time = EXCLUDED.resolved_end_time,
                resolved_duration_sec = EXCLUDED.resolved_duration_sec,
                window_source = EXCLUDED.window_source,
                start_fuel_level_pct = EXCLUDED.start_fuel_level_pct,
                end_fuel_level_pct = EXCLUDED.end_fuel_level_pct,
                fuel_amount_l = EXCLUDED.fuel_amount_l,
                start_energy_kwh = EXCLUDED.start_energy_kwh,
                end_energy_kwh = EXCLUDED.end_energy_kwh,
                energy_charged_kwh = EXCLUDED.energy_charged_kwh,
                avg_charging_kw = EXCLUDED.avg_charging_kw,
                range_at_start_km = EXCLUDED.range_at_start_km,
                range_at_end_km = EXCLUDED.range_at_end_km,
                cost_amount = EXCLUDED.cost_amount,
                cost_currency = EXCLUDED.cost_currency,
                outside_temperature = EXCLUDED.outside_temperature,
                odometer_km = EXCLUDED.odometer_km,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                raw = EXCLUDED.raw,
                updated_at = now()
            """,
            refuel_id,
            str(vehicle_id),
            _text(refuel.get("tripId")),
            str(kind).lower() if kind is not None else None,
            _flag(refuel.get("isActive")),
            parse_ts(first(refuel, "startTime", "startedAt")),
            parse_ts(first(refuel, "endTime", "endedAt")),
            _int(first(refuel, "durationSec", "durationSeconds")),
            parse_ts(refuel.get("resolvedStartTime")),
            parse_ts(refuel.get("resolvedEndTime")),
            _int(refuel.get("resolvedDurationSec")),
            _text(refuel.get("windowSource")),
            num(refuel.get("startFuelLevelPct")),
            num(refuel.get("endFuelLevelPct")),
            num(refuel.get("fuelAmountL")),
            num(refuel.get("startEnergyKwh")),
            num(refuel.get("endEnergyKwh")),
            num(refuel.get("energyChargedKwh")),
            num(refuel.get("avgChargingKw")),
            num(refuel.get("rangeAtStartKm")),
            num(refuel.get("rangeAtEndKm")),
            num(refuel.get("costAmount")),
            _text(refuel.get("costCurrency")),
            num(refuel.get("outsideTemperature")),
            num(first(refuel, "odometerKm", "odometer")),
            num(nested(refuel, "location", "latitude")),
            num(nested(refuel, "location", "longitude")),
            json.dumps(refuel),
        )
        return refuel_id

    async def known_ids(self, table: str, id_column: str, vehicle_id: str) -> set[str]:
        """Every stored record id for one vehicle, used to stop incremental walks."""
        if table not in {"trip", "refuel"} or id_column not in {"trip_id", "refuel_id"}:
            raise ValueError(f"Unsupported table/column: {table}/{id_column}")
        rows = await self._pool.fetch(
            f"SELECT {id_column} FROM aximote.{table} WHERE vehicle_id = $1",
            vehicle_id,
        )
        return {row[0] for row in rows}


def _text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _int(value: Any) -> int | None:
    parsed = num(value)
    return int(parsed) if parsed is not None else None


def _flag(value: Any) -> bool | None:
    return None if value is None else bool(value)
