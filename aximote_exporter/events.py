"""Trip and refuel ingestion into Postgres."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .api import AximoteApiClient
from .metrics import events_ingested
from .store import Store, record_id

_LOGGER = logging.getLogger(__name__)

TRIPS_PATH = "/api/public/v1/trips"
REFUELS_PATH = "/api/public/v1/refuels"


@dataclass(frozen=True)
class HistoryKind:
    """One history endpoint and where its records land."""

    name: str
    path: str
    table: str
    id_column: str
    extra_id_keys: tuple[str, ...]


TRIPS = HistoryKind("trip", TRIPS_PATH, "trip", "trip_id", ("tripId",))
REFUELS = HistoryKind("refuel", REFUELS_PATH, "refuel", "refuel_id", ("refuelId",))


async def ingest_history(
    client: AximoteApiClient,
    store: Store,
    kind: HistoryKind,
    vehicle_id: str,
    *,
    page_size: int,
    max_pages: int,
    allow_backfill: bool,
) -> int:
    """Read one history endpoint for one vehicle and upsert what it returns.

    Two modes, chosen by whether the table already holds anything for this vehicle:

    * **backfill** — walk every page to the end (or the page cap) once, so the
      dashboards start with real history instead of filling up over months.
    * **incremental** — walk from the newest record and stop at the first page that
      contains no id we have not already stored. Steady state is one request per
      endpoint per cycle.

    Records already stored are still upserted when encountered, because a trip's
    totals can be revised after it ends.
    """
    known = await store.known_ids(kind.table, kind.id_column, vehicle_id)
    backfilling = allow_backfill and not known

    writer = _writer_for(store, kind)
    new_count = 0
    page_index = 0

    async for page in client.async_iter_history(
        kind.path,
        vehicle_id,
        page_size=page_size,
        max_pages=max_pages,
    ):
        page_index += 1
        fresh_in_page = 0

        for item in page:
            item_id = record_id(item, *kind.extra_id_keys)
            is_new = item_id is not None and item_id not in known
            written = await writer(item)
            if written is None:
                continue
            known.add(written)
            if is_new:
                fresh_in_page += 1
                new_count += 1

        if not backfilling and fresh_in_page == 0:
            break

    events_ingested.labels(kind=kind.name).inc(new_count)
    if new_count:
        _LOGGER.info(
            "Ingested %d new %s record(s) for vehicle %s across %d page(s)%s",
            new_count,
            kind.name,
            vehicle_id,
            page_index,
            " (backfill)" if backfilling else "",
        )
    return new_count


def _writer_for(
    store: Store,
    kind: HistoryKind,
) -> Callable[[dict[str, Any]], Awaitable[str | None]]:
    if kind is TRIPS:
        return store.upsert_trip
    if kind is REFUELS:
        return store.upsert_refuel
    raise ValueError(f"No writer for history kind {kind.name}")
