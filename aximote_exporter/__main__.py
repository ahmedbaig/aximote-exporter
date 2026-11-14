"""Service entrypoint: one process, two loops.

The state loop serves Prometheus; the event loop fills Postgres. They run at
different intervals because they answer different questions -- "where is the car
now" versus "what did this month cost" -- and share one API client so the request
rate against a 429-capable API stays predictable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

import aiohttp
from prometheus_client import start_http_server

from .api import (
    AximoteApiClient,
    AximoteAuthError,
    AximoteError,
    AximoteProRequiredError,
)
from .config import Config, ConfigError, load_config
from .events import REFUELS, TRIPS, ingest_history
from .metrics import (
    AximoteCollector,
    poll_duration,
    poll_errors,
    poll_total,
    register_collector,
    state_samples_written,
)
from .state import build_snapshot, build_state_sample, fetch_vehicle_states
from .store import Store

_LOGGER = logging.getLogger("aximote")


class VehicleRegistry:
    """The vehicle list the state loop last saw, shared with the event loop.

    The event loop needs vehicle ids but should not issue its own ``/vehicles``
    request every cycle, so it reads whatever the state loop most recently fetched.
    """

    def __init__(self) -> None:
        self._vehicles: list[dict[str, Any]] = []
        self._ready = asyncio.Event()

    def publish(self, vehicles: list[dict[str, Any]]) -> None:
        self._vehicles = vehicles
        self._ready.set()

    @property
    def vehicles(self) -> list[dict[str, Any]]:
        return list(self._vehicles)

    async def wait_ready(self) -> None:
        await self._ready.wait()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Wait out an interval, returning early if shutdown was requested."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def _log_fatal_auth(err: AximoteError) -> None:
    """Explain an unrecoverable credential problem.

    Unlike the Home Assistant integration there is no UI to prompt for a new token,
    so the service keeps retrying and says exactly what a human has to do.
    """
    _LOGGER.error(
        "Aximote rejected the token (%s): %s. The exporter cannot re-authenticate "
        "itself -- issue a new personal access token in the Aximote app, update "
        "AXIMOTE_TOKEN and restart. Retrying meanwhile",
        err.code,
        err,
    )


async def state_loop(
    config: Config,
    client: AximoteApiClient,
    collector: AximoteCollector,
    registry: VehicleRegistry,
    store: Store | None,
    stop: asyncio.Event,
) -> None:
    """Poll live state, publish Prometheus metrics, append history rows."""
    while not stop.is_set():
        poll_total.labels(loop="state").inc()
        started = time.monotonic()
        try:
            vehicles = await client.async_list_vehicles()
            registry.publish(vehicles)
            if not vehicles:
                _LOGGER.info(
                    "Aximote returned no vehicles for this account; metrics will "
                    "report aximote_vehicles 0 until one exists",
                )

            states = await fetch_vehicle_states(client, vehicles)

            snapshots = []
            samples = []
            for vehicle in vehicles:
                vehicle_id = str(vehicle.get("id", ""))
                state = states.get(vehicle_id)
                if state is None:
                    continue
                snapshots.append(build_snapshot(vehicle, state))
                samples.append(build_state_sample(vehicle, state))

            collector.publish(snapshots, api_up=True, last_success_ts=time.time())

            if store is not None:
                for vehicle in vehicles:
                    await store.upsert_vehicle(vehicle)
                if config.state_samples and samples:
                    written = await store.insert_state_samples(samples)
                    state_samples_written.inc(written)

        except (AximoteAuthError, AximoteProRequiredError) as err:
            poll_errors.labels(code=err.code).inc()
            collector.mark_failure()
            _log_fatal_auth(err)
        except AximoteError as err:
            poll_errors.labels(code=err.code).inc()
            collector.mark_failure()
            _LOGGER.warning("State poll failed (%s): %s", err.code, err)
        except Exception:
            poll_errors.labels(code="internal").inc()
            collector.mark_failure()
            _LOGGER.exception("Unexpected error in state loop")
        finally:
            poll_duration.labels(loop="state").observe(time.monotonic() - started)

        await _sleep_or_stop(stop, config.state_interval_sec)


async def event_loop(
    config: Config,
    client: AximoteApiClient,
    registry: VehicleRegistry,
    store: Store,
    stop: asyncio.Event,
) -> None:
    """Ingest trips and refuels into Postgres."""
    await registry.wait_ready()

    while not stop.is_set():
        poll_total.labels(loop="events").inc()
        started = time.monotonic()
        try:
            for vehicle in registry.vehicles:
                vehicle_id = str(vehicle.get("id", ""))
                if not vehicle_id:
                    continue
                for kind in (TRIPS, REFUELS):
                    await ingest_history(
                        client,
                        store,
                        kind,
                        vehicle_id,
                        page_size=config.page_size,
                        max_pages=config.max_pages,
                        allow_backfill=config.backfill,
                    )
        except (AximoteAuthError, AximoteProRequiredError) as err:
            poll_errors.labels(code=err.code).inc()
            _log_fatal_auth(err)
        except AximoteError as err:
            poll_errors.labels(code=err.code).inc()
            _LOGGER.warning("Event ingest failed (%s): %s", err.code, err)
        except Exception:
            poll_errors.labels(code="internal").inc()
            _LOGGER.exception("Unexpected error in event loop")
        finally:
            poll_duration.labels(loop="events").observe(time.monotonic() - started)

        await _sleep_or_stop(stop, config.event_interval_sec)


async def run(config: Config) -> None:
    """Wire everything up and run until signalled."""
    collector = register_collector()
    start_http_server(config.metrics_port, addr=config.metrics_host)
    _LOGGER.info(
        "Serving metrics on http://%s:%d/metrics",
        config.metrics_host,
        config.metrics_port,
    )

    stop = asyncio.Event()
    running_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            running_loop.add_signal_handler(sig, stop.set)

    registry = VehicleRegistry()
    store: Store | None = None

    async with aiohttp.ClientSession() as session:
        client = AximoteApiClient(
            session,
            config.base_url,
            config.token,
            timeout_sec=config.request_timeout_sec,
        )

        # Fail loudly at startup rather than looking healthy with an unusable token.
        try:
            me = await client.async_me()
            _LOGGER.info(
                "Authenticated as %s (timezone %s)",
                me.get("email", "unknown"),
                me.get("timezone", "unknown"),
            )
        except AximoteError as err:
            poll_errors.labels(code=err.code).inc()
            _LOGGER.error("Startup identity check failed (%s): %s", err.code, err)

        if config.events_enabled:
            assert config.pg_dsn is not None
            store = await Store.connect(config.pg_dsn)
        else:
            _LOGGER.warning(
                "AXIMOTE_PG_DSN is not set: serving metrics only. Trip, refuel and "
                "location history will not be recorded",
            )

        tasks = [
            asyncio.create_task(
                state_loop(config, client, collector, registry, store, stop),
                name="state",
            ),
        ]
        if store is not None:
            tasks.append(
                asyncio.create_task(
                    event_loop(config, client, registry, store, stop),
                    name="events",
                ),
            )

        try:
            await asyncio.gather(*tasks)
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if store is not None:
                await store.close()

    _LOGGER.info("Shut down cleanly")


def main() -> int:
    try:
        config = load_config()
    except ConfigError as err:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        _LOGGER.error("Configuration error: %s", err)
        return 2

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
