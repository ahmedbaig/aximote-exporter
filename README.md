# aximote-exporter

Your car's telemetry from [Aximote](https://aximote.com) in **Prometheus**, **Grafana** and (optionally) **Postgres**: battery or fuel, range, odometer, position, trips, charges and costs, with ready-made alert rules and a 50-panel Grafana dashboard.

One small Python service polls the Aximote public API with your personal access token. Live readings become Prometheus metrics; trips, charges and the GPS track go to Postgres so you can map them and ask questions like "what did a kilometre cost last month".

Not affiliated with Aximote.

## Quick start (about 5 minutes)

You need Docker with Compose and an Aximote Pro account.

1. **Get a token.** In the Aximote app, create a personal access token. It is the only credential the exporter uses.
2. **Configure.**
   ```sh
   git clone https://github.com/ahmedbaig/aximote-exporter.git
   cd aximote-exporter
   cp .env.example .env
   # set AXIMOTE_TOKEN, change AXIMOTE_PG_PASSWORD and GRAFANA_PASSWORD, set AXIMOTE_TZ
   # and for history: AXIMOTE_PG_DSN=postgresql://aximote:<password>@postgres:5432/aximote
   ```
3. **Run everything.**
   ```sh
   docker compose --profile stack up -d
   ```
4. **Open Grafana** at http://localhost:3000 (user `admin`), folder **Aximote → Aximote — Vehicle**. Prometheus is on http://localhost:9090 and Alertmanager on http://localhost:9093.

The first start backfills your trip and charge history, then settles into one request per endpoint per cycle.

| Command | Runs |
|---|---|
| `docker compose up -d` | exporter only, metrics on `127.0.0.1:9684/metrics` |
| `docker compose --profile history up -d` | exporter + Postgres (map, trips, charges, costs) |
| `docker compose --profile stack up -d` | everything: exporter, Postgres, Prometheus, Alertmanager, Grafana |

## What you get

### Prometheus metrics

One series per vehicle, labelled `vehicle_id`, `name`, `make`, `model`:

```
aximote_battery_level_percent          aximote_location_latitude
aximote_fuel_level_percent             aximote_location_longitude
aximote_range_km                       aximote_location_accuracy_meters
aximote_odometer_km                    aximote_outside_temperature_celsius
aximote_speed_kmh                      aximote_on_trip
aximote_bearing_degrees                aximote_ignition_on
aximote_battery_capacity_kwh           aximote_fuel_capacity_liters

aximote_field_captured_timestamp_seconds{field="..."}   # capture time of each reading
aximote_api_up                         aximote_vehicles
aximote_last_success_timestamp_seconds aximote_api_errors_total{code}
aximote_polls_total{loop}              aximote_poll_duration_seconds{loop}
aximote_events_ingested_total{kind}    aximote_state_samples_written_total
```

A reading the API reports as `null` produces **no series**, not a zero. On an EV the fuel metrics are simply absent, and that is normal.

### Postgres history (schema `aximote`)

| Object | Holds |
|---|---|
| `vehicle` | vehicle profile, refreshed every poll |
| `trip` | one row per completed trip |
| `refuel` | charges and fills, with session windows |
| `state_sample` | telemetry history, the source of the map track |
| `trip_economy` | view: kWh/100 km, L/100 km, km/kWh per trip |
| `refuel_session` | view: price per kWh or litre, range added, cost per km |
| `location_track` | view: de-duplicated GPS fixes for a geomap |
| `monthly_cost` | view: spend, distance and cost per 100 km by month |

Every table keeps the raw API payload in `raw jsonb`, so new upstream fields are captured before they get a column. Clock-time analytics (first departure of the day, trips by weekday) use `AXIMOTE_TZ`.

### Grafana dashboard

`grafana/aximote-vehicle.json`: live state, battery and range over time, trips, charges, charge cycles, places you stop, the map track and monthly cost. Pick the datasources and your time zone with the variables at the top.

## Already run Prometheus and Grafana?

Run the exporter alone (`docker compose up -d`, or `python -m aximote_exporter` with the env vars set) and wire it in:

```yaml
# prometheus.yml
rule_files:
  - /path/to/aximote-exporter/alerts/aximote.rules.yml
scrape_configs:
  - job_name: aximote          # keep this name, the alerts use it
    scrape_interval: 60s
    static_configs:
      - targets: ["<exporter-host>:9684"]
```

```sh
promtool check config prometheus.yml && systemctl reload prometheus
```

Then in Grafana: add a PostgreSQL datasource for the `aximote` database (if you use history), and **Dashboards → New → Import** `grafana/aximote-vehicle.json`. For your own Postgres, run `sql/000_bootstrap.sql` once as a superuser to create the role and database; the exporter creates the schema itself on start.

## Alerting

How it fits together: **Prometheus** evaluates the rules in `alerts/aximote.rules.yml` every minute → a rule that stays true for its `for:` time **fires** → **Alertmanager** groups it and sends it to a **receiver** (Discord, email, Telegram, ntfy…).

### The rules

| Alert | Fires when | What to do |
|---|---|---|
| `AximoteExporterDown` | Prometheus can't scrape the exporter for 10 min | check the container: `docker compose logs aximote-exporter` |
| `AximoteApiFailing` | API polls failing for 15 min | usually network or an Aximote outage |
| `AximoteTokenRejected` | API answered 401 / "Pro required" | create a new token, update `.env`, restart |
| `AximoteRateLimited` | more than 3× HTTP 429 in 30 min | raise `AXIMOTE_STATE_INTERVAL` |
| `AximoteNoVehicles` | the account shows no vehicles for 1 h | check the car is still linked in the app |
| `AximoteStatusStale` | the car's status is over 2 h old | the in-car agent stopped reporting |
| `AximoteLocationStale` | the GPS fix is over 6 h old | position on the map is out of date |
| `AximoteBatteryLow` | battery under 20 % for 30 min | charge |
| `AximoteFuelLow` | fuel under 15 % for 30 min | refuel |
| `AximoteRangeLow` | range under 50 km for 30 min | charge or refuel before the next trip |

Change a threshold by editing the `expr:` line (for example `aximote_battery_level_percent < 30`), then reload Prometheus or `docker compose --profile stack restart prometheus`.

### Why each reading has its own freshness alert

The API timestamps every reading separately, and they disagree by hours:

```
capturedAt:          04:18:56Z   <- status
batteryCapturedAt:   04:30:39Z
locationCapturedAt:  02:17:45Z   <- GPS two hours older
```

A check on `capturedAt` alone would be green while the map is two hours stale, so the age of each reading is exported and alerted on individually. Treat `ignitionOn` with care too: it has been seen `true` on a parked car with a two-hour-old ignition timestamp.

### Send alerts somewhere

Out of the box alerts appear only in the Alertmanager UI. To get notified, open `examples/stack/alertmanager.yml`, uncomment one receiver, set `route.receiver` to its name, and restart:

```yaml
route:
  receiver: discord
receivers:
  - name: discord
    discord_configs:
      - webhook_url: https://discord.com/api/webhooks/<id>/<token>
```

```sh
docker compose --profile stack restart alertmanager
```

Discord, email, Telegram and ntfy examples are in the file. The default config already repeats token and API problems hourly and mutes staleness alerts while the exporter itself is down.

### Test it

```sh
# rules are valid
docker compose --profile stack exec prometheus promtool check rules /etc/prometheus/rules/aximote.rules.yml

# send a fake alert through Alertmanager to your receiver
docker compose --profile stack exec alertmanager \
  amtool alert add AximoteTest severity=warning summary="Test from aximote-exporter" \
  --alertmanager.url=http://localhost:9093

# see what is firing
open http://localhost:9090/alerts
```

Silence a noisy alert from the Alertmanager UI (**New silence**), or with `amtool silence add alertname=AximoteBatteryLow --duration=12h --alertmanager.url=http://localhost:9093`.

## Configuration

All environment variables, with defaults in `.env.example`:

| Variable | Default | |
|---|---|---|
| `AXIMOTE_TOKEN` | required | personal access token |
| `AXIMOTE_PG_DSN` | empty | Postgres for history; empty = metrics only |
| `AXIMOTE_STATE_INTERVAL` | `180` | seconds between live-state polls |
| `AXIMOTE_EVENT_INTERVAL` | `300` | seconds between trip/charge polls |
| `AXIMOTE_METRICS_HOST` / `_PORT` | `0.0.0.0` / `9684` | metrics listener |
| `AXIMOTE_BACKFILL` | `true` | walk full history on first start |
| `AXIMOTE_STATE_SAMPLES` | `true` | record telemetry history (needed for the map) |
| `AXIMOTE_PAGE_SIZE` / `_MAX_PAGES` | `100` / `200` | history paging |
| `AXIMOTE_REQUEST_TIMEOUT` | `30` | seconds |
| `AXIMOTE_LOG_LEVEL` | `INFO` | |
| `AXIMOTE_TZ` | `UTC` | time zone for clock-time analytics (bundled Postgres) |

Polling faster than the defaults buys nothing: the in-car agent reports far less often than every 3 minutes, and the API rate-limits.

## Privacy and security

- **`/metrics` contains your car's live GPS position.** The compose file binds every port to `127.0.0.1`. Do not expose the exporter, Prometheus or Grafana to the internet without authentication in front.
- The token **is** your identity to the API: whoever has it can read your car. Keep `.env` out of git (it is in `.gitignore`), and rotate the token if it was ever pasted somewhere that keeps history.
- The service never needs root and makes only outbound HTTPS calls.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `AximoteTokenRejected`, logs show 401 or 402 | token revoked, or Aximote Pro lapsed. Create a new token, restart |
| Logs show 429 | polling too fast; raise the intervals |
| No fuel metrics | normal on an EV, the API reports fuel as null |
| Map, trips and cost panels empty | `AXIMOTE_PG_DSN` not set, or the history profile not running |
| Clock-time panels look shifted | set `AXIMOTE_TZ` (Postgres) and the dashboard's **Time zone** variable |
| Dashboard panels say "no data" | check the datasource variables at the top of the dashboard |

## Development

```sh
pip install -r requirements.txt
AXIMOTE_TOKEN=... python -m aximote_exporter
python ci/check_dashboard.py grafana/aximote-vehicle.json
```

## License

MIT
