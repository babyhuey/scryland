"""Tests for eBay orders client — pagination contract + flattening."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx

from scryland.config import ScrylandConfig
from scryland.ebay.orders import EbayOrdersClient


def _make_client(transport: httpx.MockTransport) -> EbayOrdersClient:
    config = ScrylandConfig()
    auth = MagicMock()
    auth.api_base = "https://api.ebay.com"
    auth.access_token = AsyncMock(return_value="tok")
    c = EbayOrdersClient(config, auth, "pw")
    c._http = httpx.AsyncClient(
        base_url="https://api.ebay.com",
        transport=transport,
        timeout=5.0,
    )
    return c


class TestIterRecentOrders:
    async def test_single_page_terminates(self):
        """total matches seen → stop after one page."""

        def handler(req):
            return httpx.Response(
                200,
                json={
                    "orders": [{"orderId": "A"}, {"orderId": "B"}],
                    "total": 2,
                },
            )

        client = _make_client(httpx.MockTransport(handler))
        orders = await client.iter_recent_orders()
        assert len(orders) == 2
        await client._http.aclose()

    async def test_follows_next_href(self):
        state = {"page": 0}

        def handler(req):
            state["page"] += 1
            if state["page"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "orders": [{"orderId": "A"}],
                        "next": "https://api.ebay.com/sell/fulfillment/v1/order?offset=50",
                        "total": 2,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "orders": [{"orderId": "B"}],
                    "total": 2,
                },
            )

        client = _make_client(httpx.MockTransport(handler))
        orders = await client.iter_recent_orders()
        assert [o["orderId"] for o in orders] == ["A", "B"]
        await client._http.aclose()

    async def test_empty_page_with_has_next_continues(self):
        """eBay sometimes returns empty mid-stream; shouldn't stop early."""
        state = {"page": 0}

        def handler(req):
            state["page"] += 1
            if state["page"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "orders": [],
                        "next": "https://api.ebay.com/sell/fulfillment/v1/order?offset=50",
                        "total": 1,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "orders": [{"orderId": "A"}],
                    "total": 1,
                },
            )

        client = _make_client(httpx.MockTransport(handler))
        orders = await client.iter_recent_orders()
        assert len(orders) == 1
        assert orders[0]["orderId"] == "A"
        await client._http.aclose()

    async def test_malformed_response_breaks_with_error(self, caplog):
        import logging

        def handler(req):
            # No 'orders' and no 'total' — unexpected shape
            return httpx.Response(200, json={"message": "maintenance"})

        client = _make_client(httpx.MockTransport(handler))
        with caplog.at_level(logging.ERROR, logger="scryland"):
            orders = await client.iter_recent_orders()
        assert orders == []
        assert any("unexpected shape" in r.message for r in caplog.records)
        await client._http.aclose()

    async def test_204_returns_empty(self):
        def handler(req):
            return httpx.Response(204)

        client = _make_client(httpx.MockTransport(handler))
        orders = await client.iter_recent_orders()
        assert orders == []
        await client._http.aclose()

    async def test_page_ceiling_logs_error(self, caplog):
        """Hitting the limit is a data-loss signal, not a warning."""
        import logging

        def handler(req):
            # Always return a full page with more remaining to loop forever.
            return httpx.Response(
                200,
                json={
                    "orders": [{"orderId": f"O{i}"} for i in range(50)],
                    "next": "https://api.ebay.com/…",
                    "total": 100000,
                },
            )

        client = _make_client(httpx.MockTransport(handler))
        with caplog.at_level(logging.ERROR, logger="scryland"):
            orders = await client.iter_recent_orders(limit_pages=3)
        assert len(orders) == 150  # 3 pages × 50
        assert any("ceiling" in r.message for r in caplog.records)
        await client._http.aclose()

    async def test_http_error_returns_partial(self, caplog):
        import logging

        state = {"page": 0}

        def handler(req):
            state["page"] += 1
            if state["page"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "orders": [{"orderId": "A"}],
                        "next": "https://api.ebay.com/…",
                        "total": 2,
                    },
                )
            return httpx.Response(500, text="boom")

        client = _make_client(httpx.MockTransport(handler))
        with caplog.at_level(logging.WARNING, logger="scryland"):
            orders = await client.iter_recent_orders()
        assert len(orders) == 1
        await client._http.aclose()
