-- Local time comes from the database setting aximote.tz (an IANA zone such as
-- 'Europe/London' or 'Australia/Sydney'), defaulting to UTC:
--   ALTER DATABASE aximote SET aximote.tz = 'Australia/Sydney';
CREATE OR REPLACE FUNCTION aximote.tz() RETURNS text
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce(nullif(current_setting('aximote.tz', true), ''), 'UTC') $$;

-- Analytics views. Idempotent; applied at startup after 001_schema.sql.
--
-- These exist so the dashboard's SQL stays short enough to read in the panel
-- editor. Every one of them is a view, not a table, so a corrected trip row is
-- reflected immediately with nothing to recompute.
--
-- reported by GET /api/public/v1/me, and it has to be stated explicitly: the
-- stored timestamps are UTC, so grouping by hour without converting puts the
-- morning commute in the middle of the night.

-- Clustering granularity for "places". Two decimal places of latitude and
-- longitude is roughly a 1.1 km cell, which is what it takes to merge one
-- destination into one row: at three decimals (~110 m) a single driveway split
-- across four adjacent cells with 23, 16, 8 and 6 visits.
CREATE OR REPLACE VIEW aximote.place AS
WITH points AS (
    SELECT
        vehicle_id,
        round(start_latitude::numeric, 2)  AS lat,
        round(start_longitude::numeric, 2) AS lon,
        start_locality                     AS locality,
        start_time                         AS ts,
        'departure'                        AS kind,
        distance_km
    FROM aximote.trip
    WHERE start_latitude IS NOT NULL AND start_longitude IS NOT NULL
    UNION ALL
    SELECT
        vehicle_id,
        round(end_latitude::numeric, 2),
        round(end_longitude::numeric, 2),
        end_locality,
        end_time,
        'arrival',
        distance_km
    FROM aximote.trip
    WHERE end_latitude IS NOT NULL AND end_longitude IS NOT NULL
)
SELECT
    p.vehicle_id,
    v.name                                            AS vehicle_name,
    p.lat::double precision                           AS latitude,
    p.lon::double precision                           AS longitude,
    mode() WITHIN GROUP (ORDER BY p.locality)         AS locality,
    count(*)                                          AS visits,
    count(*) FILTER (WHERE p.kind = 'departure')      AS departures,
    count(*) FILTER (WHERE p.kind = 'arrival')        AS arrivals,
    min(p.ts)                                         AS first_seen,
    max(p.ts)                                         AS last_seen,
    avg(p.distance_km)                                AS avg_trip_km,
    -- Mean clock time of departures from here, as seconds past local midnight.
    -- Grafana renders this with unit "clock" / "s" depending on the panel.
    avg(
        extract(epoch FROM (p.ts AT TIME ZONE aximote.tz())::time)
    ) FILTER (WHERE p.kind = 'departure')             AS avg_departure_sec,
    avg(
        extract(epoch FROM (p.ts AT TIME ZONE aximote.tz())::time)
    ) FILTER (WHERE p.kind = 'arrival')               AS avg_arrival_sec
FROM points p
LEFT JOIN aximote.vehicle v USING (vehicle_id)
GROUP BY p.vehicle_id, v.name, p.lat, p.lon;

-- Trips as two-point vectors, for drawing start-to-end lines on a map. The API
-- returns no route polyline, so this is the coarse shape of a journey; the fine
-- path comes from aximote.location_track once enough fixes accumulate.
-- Consume ordered by (trip_id, seq).
CREATE OR REPLACE VIEW aximote.trip_vector AS
SELECT
    t.trip_id,
    t.vehicle_id,
    v.name AS vehicle_name,
    t.start_time,
    t.end_time,
    t.distance_km,
    t.energy_consumed_kwh,
    t.eco_score,
    t.avg_speed_kmh,
    COALESCE(t.start_locality, '?') || ' → ' || COALESCE(t.end_locality, '?') AS route,
    1                AS seq,
    t.start_latitude  AS latitude,
    t.start_longitude AS longitude
FROM aximote.trip t
LEFT JOIN aximote.vehicle v USING (vehicle_id)
WHERE t.start_latitude IS NOT NULL AND t.end_latitude IS NOT NULL
UNION ALL
SELECT
    t.trip_id,
    t.vehicle_id,
    v.name,
    t.start_time,
    t.end_time,
    t.distance_km,
    t.energy_consumed_kwh,
    t.eco_score,
    t.avg_speed_kmh,
    COALESCE(t.start_locality, '?') || ' → ' || COALESCE(t.end_locality, '?'),
    2,
    t.end_latitude,
    t.end_longitude
FROM aximote.trip t
LEFT JOIN aximote.vehicle v USING (vehicle_id)
WHERE t.start_latitude IS NOT NULL AND t.end_latitude IS NOT NULL;

-- One row per trip with local calendar fields already extracted, so the
-- when-do-I-drive panels do not each repeat the timezone conversion.
CREATE OR REPLACE VIEW aximote.trip_local AS
SELECT
    t.trip_id,
    t.vehicle_id,
    v.name AS vehicle_name,
    t.start_time,
    (t.start_time AT TIME ZONE aximote.tz())                      AS local_start,
    (t.end_time   AT TIME ZONE aximote.tz())                      AS local_end,
    (t.start_time AT TIME ZONE aximote.tz())::date                AS local_date,
    extract(dow  FROM t.start_time AT TIME ZONE aximote.tz())::int AS dow,
    to_char(t.start_time AT TIME ZONE aximote.tz(), 'Dy')          AS dow_name,
    extract(hour FROM t.start_time AT TIME ZONE aximote.tz())::int AS hour,
    extract(epoch FROM (t.start_time AT TIME ZONE aximote.tz())::time) AS seconds_into_day,
    t.distance_km,
    t.duration_sec,
    t.avg_speed_kmh,
    t.energy_consumed_kwh,
    t.co2_kg,
    t.eco_score,
    t.speed_score,
    t.consistency_score,
    t.outside_temperature,
    t.start_locality,
    t.end_locality,
    100.0 * t.energy_consumed_kwh / NULLIF(t.distance_km, 0) AS kwh_per_100km
FROM aximote.trip t
LEFT JOIN aximote.vehicle v USING (vehicle_id)
WHERE t.start_time IS NOT NULL;

-- The first time the car moved on each local day. Answers "when do I actually
-- leave in the morning", which the per-trip hour histogram cannot: a day with
-- eleven trips would otherwise outvote a day with one.
CREATE OR REPLACE VIEW aximote.day_first_departure AS
SELECT DISTINCT ON (vehicle_id, local_date)
    vehicle_id,
    vehicle_name,
    local_date,
    dow,
    dow_name,
    local_start,
    seconds_into_day,
    start_locality,
    end_locality,
    distance_km
FROM aximote.trip_local
ORDER BY vehicle_id, local_date, local_start;

-- How long the car sits between trips, and where. Useful for telling a genuine
-- destination from a traffic-light pause that the API split into two trips.
CREATE OR REPLACE VIEW aximote.parked_interval AS
WITH ordered AS (
    SELECT
        vehicle_id,
        trip_id,
        end_time,
        end_locality,
        end_latitude,
        end_longitude,
        lead(start_time)  OVER w AS next_start,
        lead(trip_id)     OVER w AS next_trip_id
    FROM aximote.trip
    WHERE start_time IS NOT NULL
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY start_time)
)
SELECT
    vehicle_id,
    trip_id,
    next_trip_id,
    end_time                                              AS parked_from,
    next_start                                            AS parked_until,
    extract(epoch FROM (next_start - end_time))           AS parked_sec,
    end_locality                                          AS locality,
    end_latitude                                          AS latitude,
    end_longitude                                         AS longitude
FROM ordered
WHERE next_start IS NOT NULL
  AND next_start > end_time;

-- A charge cycle: everything driven between the end of one charge and the start
-- of the next. This is what answers "how far did I get on one charge", which no
-- single API record contains -- trips and charges are separate endpoints with no
-- link between them, so the window has to be reconstructed from their times.
--
-- The newest cycle is still open: its next charge has not happened, so soc_end
-- and anything derived from it are NULL rather than wrong.
CREATE OR REPLACE VIEW aximote.charge_cycle AS
WITH sessions AS (
    SELECT
        refuel_id,
        vehicle_id,
        start_time,
        end_time,
        end_fuel_level_pct,
        energy_charged_kwh,
        lead(start_time) OVER w            AS next_charge_start,
        lead(start_fuel_level_pct) OVER w  AS next_charge_soc
    FROM aximote.refuel
    WHERE end_time IS NOT NULL
    WINDOW w AS (PARTITION BY vehicle_id ORDER BY start_time)
)
SELECT
    s.refuel_id                                   AS cycle_id,
    s.vehicle_id,
    v.name                                        AS vehicle_name,
    s.end_time                                    AS charged_at,
    s.next_charge_start                           AS ended_at,
    s.energy_charged_kwh                          AS charge_kwh,
    s.end_fuel_level_pct                          AS soc_start,
    s.next_charge_soc                             AS soc_end,
    s.end_fuel_level_pct - s.next_charge_soc      AS soc_used,
    count(t.trip_id)                              AS trips,
    sum(t.distance_km)                            AS distance_km,
    max(t.distance_km)                            AS longest_trip_km,
    sum(t.energy_consumed_kwh)                    AS energy_used_kwh,
    sum(t.duration_sec)                           AS driving_sec,
    count(DISTINCT t.end_locality)                AS places,
    min(t.start_time)                             AS first_trip_at,
    max(t.end_time)                               AS last_trip_at,
    -- What the whole battery would have carried at this cycle's observed rate.
    -- Optimistic: it extrapolates from the part of the pack actually used.
    100.0 * sum(t.distance_km)
        / NULLIF(s.end_fuel_level_pct - s.next_charge_soc, 0) AS implied_full_range_km
FROM sessions s
LEFT JOIN aximote.vehicle v ON v.vehicle_id = s.vehicle_id
LEFT JOIN aximote.trip t
       ON t.vehicle_id = s.vehicle_id
      AND t.start_time >= s.end_time
      AND (s.next_charge_start IS NULL OR t.start_time < s.next_charge_start)
GROUP BY
    s.refuel_id, s.vehicle_id, v.name, s.end_time, s.next_charge_start,
    s.energy_charged_kwh, s.end_fuel_level_pct, s.next_charge_soc;

-- Every trip tagged with the charge cycle it belongs to, so a map or table can
-- show exactly where one charge was spent.
CREATE OR REPLACE VIEW aximote.charge_cycle_trip AS
WITH sessions AS (
    SELECT
        refuel_id,
        vehicle_id,
        end_time,
        lead(start_time) OVER (PARTITION BY vehicle_id ORDER BY start_time) AS next_charge_start
    FROM aximote.refuel
    WHERE end_time IS NOT NULL
)
SELECT
    s.refuel_id AS cycle_id,
    t.trip_id,
    t.vehicle_id,
    v.name      AS vehicle_name,
    t.start_time,
    t.end_time,
    t.distance_km,
    t.energy_consumed_kwh,
    t.eco_score,
    t.avg_speed_kmh,
    t.outside_temperature,
    t.start_locality,
    t.end_locality,
    COALESCE(t.start_locality, '?') || ' → ' || COALESCE(t.end_locality, '?') AS route,
    t.start_latitude,
    t.start_longitude,
    t.end_latitude,
    t.end_longitude
FROM sessions s
JOIN aximote.trip t
       ON t.vehicle_id = s.vehicle_id
      AND t.start_time >= s.end_time
      AND (s.next_charge_start IS NULL OR t.start_time < s.next_charge_start)
-- Explicit ON, not USING: both `s` and `t` carry vehicle_id, and USING against a
-- left table that already has the column twice is an error.
LEFT JOIN aximote.vehicle v ON v.vehicle_id = t.vehicle_id;
