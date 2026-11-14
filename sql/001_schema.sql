-- Aximote homelab schema.
--
-- Idempotent: safe to re-run. The loader applies this at startup.
--
-- Column set was derived by probing the live API, not from the Home Assistant
-- integration, which exposes only a subset (it reads none of the refuel energy
-- boundaries, the per-reading capture times, or trip locality). Every table still
-- keeps a `raw jsonb` copy of the payload, so a field added upstream is captured
-- and can be promoted to a column later without re-reading history.

CREATE SCHEMA IF NOT EXISTS aximote;

-- Vehicle dimension. Updated every poll; one row per vehicle, ever.
-- `vin` is stored here but deliberately never becomes a Prometheus label.
CREATE TABLE IF NOT EXISTS aximote.vehicle (
    vehicle_id            text PRIMARY KEY,
    name                  text,
    make                  text,
    model                 text,
    year                  integer,
    vin                   text,
    fuel_type             text,
    fuel_capacity_l       double precision,
    battery_capacity_kwh  double precision,
    odometer_km           double precision,
    updated_at            timestamptz,
    raw                   jsonb NOT NULL,
    first_seen_at         timestamptz NOT NULL DEFAULT now(),
    last_seen_at          timestamptz NOT NULL DEFAULT now()
);

-- Completed trips. `trip_id` is the API's own id, so re-reading a page is a no-op
-- and a trip whose totals are revised after it ends updates in place.
--
-- The API returns only start and end points, no route polyline, so a map of the
-- actual path has to come from aximote.state_sample.
CREATE TABLE IF NOT EXISTS aximote.trip (
    trip_id              text PRIMARY KEY,
    vehicle_id           text NOT NULL,
    start_time           timestamptz,
    end_time             timestamptz,
    duration_sec         bigint,
    distance_km          double precision,
    avg_speed_kmh        double precision,
    fuel_consumed_l      double precision,
    energy_consumed_kwh  double precision,
    co2_kg               double precision,
    eco_score            double precision,
    speed_score          double precision,
    consistency_score    double precision,
    outside_temperature  double precision,
    start_latitude       double precision,
    start_longitude      double precision,
    start_country        text,
    start_locality       text,
    end_latitude         double precision,
    end_longitude        double precision,
    end_country          text,
    end_locality         text,
    tags                 text[],
    raw                  jsonb NOT NULL,
    ingested_at          timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS trip_vehicle_start_idx
    ON aximote.trip (vehicle_id, start_time DESC);

-- Refuels and charges share one table; `kind` separates them ('fuel' / 'charge').
--
-- Both a reported and a "resolved" window are kept: the API returns each
-- separately and they disagree (a charge reported as 3422 s resolved to 3510 s),
-- so collapsing them would silently pick one definition of session length.
CREATE TABLE IF NOT EXISTS aximote.refuel (
    refuel_id              text PRIMARY KEY,
    vehicle_id             text NOT NULL,
    trip_id                text,
    kind                   text,
    is_active              boolean,
    start_time             timestamptz,
    end_time               timestamptz,
    duration_sec           bigint,
    resolved_start_time    timestamptz,
    resolved_end_time      timestamptz,
    resolved_duration_sec  bigint,
    window_source          text,
    start_fuel_level_pct   double precision,
    end_fuel_level_pct     double precision,
    fuel_amount_l          double precision,
    start_energy_kwh       double precision,
    end_energy_kwh         double precision,
    energy_charged_kwh     double precision,
    avg_charging_kw        double precision,
    range_at_start_km      double precision,
    range_at_end_km        double precision,
    cost_amount            double precision,
    cost_currency          text,
    outside_temperature    double precision,
    odometer_km            double precision,
    latitude               double precision,
    longitude              double precision,
    raw                    jsonb NOT NULL,
    ingested_at            timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS refuel_vehicle_start_idx
    ON aximote.refuel (vehicle_id, start_time DESC);

-- Telemetry history, one row per distinct reading the car reported.
-- Prometheus holds the same numbers for alerting; this table exists so a Grafana
-- geomap can draw an actual track, and so location history outlives Prometheus
-- retention.
--
-- The primary key is the car's own capture time, not the poll time, so polling a
-- car that has not reported since the last poll inserts nothing. The row rate
-- follows how often the car reports, which is well under the poll rate: measured
-- on a parked car, four polls produced one row.
--
-- `location_captured_at` is separate from `captured_at` on purpose: the GPS fix is
-- routinely hours older than the status poll, so without it a parked car's repeated
-- identical fix cannot be told apart from real movement.
CREATE TABLE IF NOT EXISTS aximote.state_sample (
    vehicle_id            text NOT NULL,
    captured_at           timestamptz NOT NULL,
    location_captured_at  timestamptz,
    latitude              double precision,
    longitude             double precision,
    accuracy_m            double precision,
    speed_kmh             double precision,
    bearing_deg           double precision,
    fuel_level_pct        double precision,
    battery_level_pct     double precision,
    range_km              double precision,
    odometer_km           double precision,
    outside_temperature   double precision,
    on_trip               boolean,
    ignition_on           boolean,
    trip_id               text,
    PRIMARY KEY (vehicle_id, captured_at)
);

CREATE INDEX IF NOT EXISTS state_sample_trip_idx
    ON aximote.state_sample (trip_id, captured_at)
    WHERE trip_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS state_sample_location_idx
    ON aximote.state_sample (vehicle_id, location_captured_at DESC)
    WHERE latitude IS NOT NULL;

-- One row per distinct GPS fix, for maps. Polling every 180s against a fix that
-- updates far less often produces long runs of identical points; DISTINCT ON the
-- fix time collapses them so a geomap draws a track instead of a pile.
CREATE OR REPLACE VIEW aximote.location_track AS
SELECT DISTINCT ON (s.vehicle_id, s.location_captured_at)
    s.vehicle_id,
    v.name AS vehicle_name,
    s.location_captured_at AS fixed_at,
    s.captured_at          AS polled_at,
    s.latitude,
    s.longitude,
    s.accuracy_m,
    s.speed_kmh,
    s.bearing_deg,
    s.battery_level_pct,
    s.range_km,
    s.on_trip,
    s.trip_id,
    EXTRACT(EPOCH FROM (s.captured_at - s.location_captured_at)) AS fix_age_sec
FROM aximote.state_sample s
LEFT JOIN aximote.vehicle v USING (vehicle_id)
WHERE s.latitude IS NOT NULL
  AND s.longitude IS NOT NULL
  AND s.location_captured_at IS NOT NULL
ORDER BY s.vehicle_id, s.location_captured_at, s.captured_at;

-- Per-trip derived economy. A view, not columns, so a corrected trip row needs no
-- recomputation. NULLIF guards the division when a trip has no distance.
CREATE OR REPLACE VIEW aximote.trip_economy AS
SELECT
    t.trip_id,
    t.vehicle_id,
    v.name AS vehicle_name,
    t.start_time,
    t.end_time,
    t.distance_km,
    t.duration_sec,
    t.avg_speed_kmh,
    t.fuel_consumed_l,
    t.energy_consumed_kwh,
    t.co2_kg,
    t.eco_score,
    t.speed_score,
    t.consistency_score,
    t.outside_temperature,
    t.start_locality,
    t.end_locality,
    100.0 * t.fuel_consumed_l     / NULLIF(t.distance_km, 0) AS liters_per_100km,
    100.0 * t.energy_consumed_kwh / NULLIF(t.distance_km, 0) AS kwh_per_100km,
    t.distance_km / NULLIF(t.energy_consumed_kwh, 0)         AS km_per_kwh
FROM aximote.trip t
LEFT JOIN aximote.vehicle v USING (vehicle_id);

-- Charge and fill sessions with their own derived rates.
CREATE OR REPLACE VIEW aximote.refuel_session AS
SELECT
    r.refuel_id,
    r.vehicle_id,
    v.name AS vehicle_name,
    r.kind,
    r.start_time,
    r.end_time,
    COALESCE(r.resolved_duration_sec, r.duration_sec) AS duration_sec,
    r.window_source,
    r.start_fuel_level_pct,
    r.end_fuel_level_pct,
    r.end_fuel_level_pct - r.start_fuel_level_pct     AS level_added_pct,
    r.fuel_amount_l,
    r.energy_charged_kwh,
    r.avg_charging_kw,
    r.range_at_start_km,
    r.range_at_end_km,
    r.range_at_end_km - r.range_at_start_km           AS range_added_km,
    r.cost_amount,
    r.cost_currency,
    r.outside_temperature,
    r.latitude,
    r.longitude,
    r.cost_amount / NULLIF(r.energy_charged_kwh, 0)   AS price_per_kwh,
    r.cost_amount / NULLIF(r.fuel_amount_l, 0)        AS price_per_liter,
    (r.range_at_end_km - r.range_at_start_km)
        / NULLIF(r.energy_charged_kwh, 0)             AS range_km_per_kwh,
    r.cost_amount
        / NULLIF(r.range_at_end_km - r.range_at_start_km, 0) AS cost_per_range_km
FROM aximote.refuel r
LEFT JOIN aximote.vehicle v USING (vehicle_id);

-- What a kilometre actually costs. Refuel cost cannot be attributed to a single
-- trip, so this aggregates both sides by calendar month and divides the totals.
CREATE OR REPLACE VIEW aximote.monthly_cost AS
WITH trips AS (
    SELECT
        vehicle_id,
        date_trunc('month', start_time) AS month,
        sum(distance_km)                AS distance_km,
        count(*)                        AS trip_count,
        sum(duration_sec)               AS duration_sec,
        sum(fuel_consumed_l)            AS fuel_consumed_l,
        sum(energy_consumed_kwh)        AS energy_consumed_kwh,
        sum(co2_kg)                     AS co2_kg,
        avg(eco_score)                  AS avg_eco_score
    FROM aximote.trip
    WHERE start_time IS NOT NULL
    GROUP BY 1, 2
),
spend AS (
    SELECT
        vehicle_id,
        date_trunc('month', start_time) AS month,
        sum(cost_amount)                AS cost_amount,
        min(cost_currency)              AS cost_currency,
        sum(fuel_amount_l)              AS fuel_purchased_l,
        sum(energy_charged_kwh)         AS energy_purchased_kwh,
        count(*)                        AS stop_count
    FROM aximote.refuel
    WHERE start_time IS NOT NULL
    GROUP BY 1, 2
)
SELECT
    COALESCE(t.vehicle_id, s.vehicle_id) AS vehicle_id,
    COALESCE(t.month, s.month)           AS month,
    t.trip_count,
    t.distance_km,
    t.duration_sec,
    t.fuel_consumed_l,
    t.energy_consumed_kwh,
    t.co2_kg,
    t.avg_eco_score,
    s.stop_count,
    s.cost_amount,
    s.cost_currency,
    s.fuel_purchased_l,
    s.energy_purchased_kwh,
    100.0 * s.cost_amount / NULLIF(t.distance_km, 0)  AS cost_per_100km,
    s.cost_amount / NULLIF(s.fuel_purchased_l, 0)     AS price_per_liter,
    s.cost_amount / NULLIF(s.energy_purchased_kwh, 0) AS price_per_kwh
FROM trips t
FULL OUTER JOIN spend s
    ON t.vehicle_id = s.vehicle_id AND t.month = s.month;
