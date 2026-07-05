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

    async def test_order_to_sales_rows_handles_dict_line_items(self):
        """eBay sometimes returns lineItems as a dict for single-item orders; must not crash."""
        from scryland.ebay.orders import order_to_sales_rows

        order = {
            "orderId": "ORDER-1",
            "buyer": {"username": "buyer1"},
            "orderPaymentStatus": "PAID",
            "creationDate": "2026-01-01T00:00:00Z",
            "pricingSummary": {
                "total": {"value": "5.00", "currency": "USD"},
                "totalMarketplaceFee": {"value": "0.50", "currency": "USD"},
                "deliveryCost": {"value": "0.99", "currency": "USD"},
            },
            "lineItems": {
                "title": "Lightning Bolt NM",
                "sku": "SKU-001",
                "quantity": "1",
                "lineItemCost": {"value": "5.00", "currency": "USD"},
            },
        }
        rows = order_to_sales_rows(order)
        assert len(rows) == 1
        assert rows[0]["product_name"] == "Lightning Bolt NM"

    async def test_short_middle_page_no_skipped_records(self):
        """A page shorter than page_size mid-stream must not cause the next
        request to jump past not-yet-retrieved records."""

        def handler(req):
            offset = int(req.url.params.get("offset", "0"))
            if offset == 0:
                orders = [{"orderId": f"O{i}"} for i in range(50)]
                return httpx.Response(200, json={"orders": orders, "total": 120})
            if offset == 50:
                # Short page — only 30 records even though 40 more remain.
                orders = [{"orderId": f"O{i}"} for i in range(50, 80)]
                return httpx.Response(200, json={"orders": orders, "total": 120})
            if offset == 80:
                orders = [{"orderId": f"O{i}"} for i in range(80, 120)]
                return httpx.Response(200, json={"orders": orders, "total": 120})
            # Any other offset means we skipped or re-fetched records.
            return httpx.Response(200, json={"orders": [], "total": 120})

        client = _make_client(httpx.MockTransport(handler))
        orders = await client.iter_recent_orders()
        ids = {o["orderId"] for o in orders}
        assert ids == {f"O{i}" for i in range(120)}
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
