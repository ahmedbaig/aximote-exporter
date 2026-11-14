"""Aximote public REST API client.

Ported from the Home Assistant integration's client (``custom_components/aximote/api.py``)
with the Home Assistant dependencies removed and history pagination added.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Keys a paginated response might use for its continuation token, in priority order.
_CURSOR_RESPONSE_KEYS = ("nextCursor", "cursor", "nextPageToken", "next")
# Query parameter name to send the continuation token back as.
_CURSOR_PARAM = "cursor"


class AximoteError(Exception):
    """Base Aximote API error."""

    # Short, stable identifier used as a Prometheus label value.
    code = "error"


class AximoteAuthError(AximoteError):
    """Invalid or revoked token (401)."""

    code = "unauthorized"


class AximoteProRequiredError(AximoteError):
    """Subscription / entitlement issue (402)."""

    code = "pro_required"


class AximoteRateLimitedError(AximoteError):
    """Rate limited (429)."""

    code = "rate_limited"


class AximoteApiError(AximoteError):
    """Other API error with a structured code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AximoteApiClient:
    """Async client for ``/api/public/v1``."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
        *,
        timeout_sec: int = 30,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                timeout=self._timeout,
            ) as resp:
                text = await resp.text()
                if resp.status == 401:
                    raise AximoteAuthError("Unauthorized")
                if resp.status == 402:
                    raise AximoteProRequiredError("Payment required")
                if resp.status == 429:
                    raise AximoteRateLimitedError("Rate limited")
                if resp.status >= 400:
                    code = "http_error"
                    message = text[:500] if text else (resp.reason or "")
                    if text:
                        try:
                            body: Any = json.loads(text)
                            if isinstance(body, dict):
                                code = str(body.get("code", code))
                                message = str(body.get("message", message))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    raise AximoteApiError(code, message)
                if not text:
                    return None
                return json.loads(text)
        except aiohttp.ClientError as err:
            raise AximoteApiError("network_error", str(err)) from err
        except TimeoutError as err:
            raise AximoteApiError("timeout", "Request timed out") from err

    async def async_me(self) -> dict[str, Any]:
        """GET /api/public/v1/me."""
        result = await self._request("GET", "/api/public/v1/me")
        if not isinstance(result, dict):
            raise AximoteApiError("invalid_response", "Unexpected /me response")
        return result

    async def async_list_vehicles(self) -> list[dict[str, Any]]:
        """GET /api/public/v1/vehicles."""
        result = await self._request("GET", "/api/public/v1/vehicles")
        if not isinstance(result, list):
            raise AximoteApiError("invalid_response", "Unexpected /vehicles response")
        return result

    async def async_get_vehicle_state(self, vehicle_id: str) -> dict[str, Any]:
        """GET /api/public/v1/vehicles/{id}/state."""
        result = await self._request(
            "GET",
            f"/api/public/v1/vehicles/{vehicle_id}/state",
        )
        if not isinstance(result, dict):
            raise AximoteApiError("invalid_response", "Unexpected /state response")
        return result

    async def async_iter_history(
        self,
        path: str,
        vehicle_id: str,
        *,
        page_size: int,
        max_pages: int,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield pages of items from a paginated history endpoint, newest first.

        Pagination is cursor-only. The response carries ``nextCursor``, which is sent
        back as ``cursor``; absence of ``nextCursor`` is the one and only end-of-data
        signal. ``offset`` is **not** supported -- the backend accepts the parameter
        and ignores it, so ``offset=1`` returns the newest record again. An
        offset-based walk would therefore re-read page one forever while appearing to
        make progress, which is why there is no fallback to it here.
        """
        cursor: str | None = None

        for page_num in range(max_pages):
            params: dict[str, Any] = {"vehicleId": vehicle_id, "limit": page_size}
            if cursor is not None:
                params[_CURSOR_PARAM] = cursor

            page = await self._request("GET", path, params=params)
            items = _page_items(page)
            if not items:
                return

            yield items

            cursor = _page_cursor(page)
            if cursor is None:
                return

            if page_num == max_pages - 1:
                _LOGGER.warning(
                    "Stopped paginating %s for vehicle %s at the %d page cap; "
                    "older records were not read. Raise AXIMOTE_MAX_PAGES to go deeper",
                    path,
                    vehicle_id,
                    max_pages,
                )

    async def async_latest(self, path: str, vehicle_id: str) -> dict[str, Any] | None:
        """First item from a history endpoint, or None."""
        page = await self._request(
            "GET",
            path,
            params={"vehicleId": vehicle_id, "limit": 1},
        )
        items = _page_items(page)
        return items[0] if items else None


def _page_items(page: Any) -> list[dict[str, Any]]:
    """Extract the item list from a page response of either shape."""
    if isinstance(page, list):
        raw = page
    elif isinstance(page, dict):
        candidate = page.get("items")
        if not isinstance(candidate, list):
            candidate = page.get("data")
        raw = candidate if isinstance(candidate, list) else []
    else:
        raw = []
    return [item for item in raw if isinstance(item, dict)]


def _page_cursor(page: Any) -> str | None:
    """Extract a continuation token from a page response, if it carries one."""
    if not isinstance(page, dict):
        return None
    for key in _CURSOR_RESPONSE_KEYS:
        value = page.get(key)
        if isinstance(value, str) and value:
            return value
    return None
