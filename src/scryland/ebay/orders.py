"""eBay Sell Fulfillment API — fetch orders/sales for our seller account."""

from __future__ import annotations

import logging

import httpx

from scryland.config import ScrylandConfig
from scryland.ebay.auth import EbayAuth

logger = logging.getLogger("scryland")


class EbayOrdersClient:
    """Minimal wrapper around GET /sell/fulfillment/v1/order."""

    def __init__(self, config: ScrylandConfig, auth: EbayAuth, passphrase: str) -> None:
        self._config = config
        self._auth = auth
        self._passphrase = passphrase
        self._http = httpx.AsyncClient(base_url=auth.api_base, timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> EbayOrdersClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def iter_recent_orders(self, *, limit_pages: int = 10) -> list[dict]:
        """Fetch recent orders (paginated). Returns a list of order dicts.

        Uses eBay's pagination contract: follow the `next` href if present,
        otherwise `total - (offset + len(page))` to know if more pages remain.
        Stops on empty page or when no more results are available.
        """
        token = await self._auth.access_token(self._passphrase)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        }
        offset = 0
        page_size = 50
        orders: list[dict] = []
        # Pre-init so the `else` branch can read them even if the loop
        # body never runs (e.g. limit_pages=0).
        total: int | None = None
        for page_num in range(limit_pages):
            r = await self._http.get(
                "/sell/fulfillment/v1/order",
                headers=headers,
                params={"limit": page_size, "offset": offset},
            )
            if r.status_code == 204:
                break
            if r.status_code >= 300:
                logger.warning("list orders failed %d: %s", r.status_code, r.text[:300])
                break
            body = r.json()
            # Guard against eBay returning an unexpected payload shape
            # (maintenance-window HTML, auth error in 200, etc.).
            if "orders" not in body and "total" not in body:
                logger.error(
                    "list orders returned unexpected shape on page %d: %s",
                    page_num,
                    str(body)[:300],
                )
                break
            page = body.get("orders") or []
            orders.extend(page)

            # Authoritative signals for "more pages exist":
            # 1. `next` href present → eBay says there's more.
            # 2. `total` > (offset + len(page)) → we haven't seen everything.
            # Check these BEFORE bailing on an empty page — eBay sometimes
            # returns empty-but-not-done pages during reconciliation.
            has_next = bool(body.get("next"))
            total = body.get("total")
            seen = offset + len(page)
            more_by_total = total is not None and total > seen
            if not (has_next or more_by_total):
                break
            offset += page_size
        else:
            # Hitting the ceiling means we demonstrably missed orders —
            # treat as an error so surrounding code can surface it.
            logger.error(
                "iter_recent_orders hit %d-page ceiling — %d orders scanned, "
                "total=%s. Bump limit_pages or investigate why there's so many.",
                limit_pages,
                len(orders),
                total,
            )
        return orders


def order_to_sales_rows(order: dict) -> list[dict]:
    """Flatten an eBay order → one sale dict per line item, matching our
    DB schema (see InventoryDB.record_order_sales).

    Fees, shipping, and order total are split across line items
    proportionally to each item's lineItemCost (not by count) so a $100
    card + $1 card doesn't attribute 50% of fees to the cheap card.
    """
    order_num = order.get("orderId", "")
    buyer = (order.get("buyer") or {}).get("username", "")
    status = order.get("orderPaymentStatus", "")
    order_date = order.get("creationDate", "")
    pricing = order.get("pricingSummary") or {}
    total = _dollars(pricing.get("total"))
    fees_total = _dollars(pricing.get("totalMarketplaceFee"))
    shipping_total = _dollars(pricing.get("deliveryCost"))

    line_items = order.get("lineItems") or []
    # Compute price-weighted share for each line item. Fall back to
    # even split when all line items are $0 (degenerate case).
    line_prices = [_dollars(li.get("lineItemCost")) for li in line_items]
    subtotal = sum(line_prices)

    rows: list[dict] = []
    for li, line_total in zip(line_items, line_prices, strict=True):
        title = li.get("title", "")
        sku = li.get("sku", "")
        qty = int(li.get("quantity") or 1)
        # `lineItemCost` is the LINE total (unit × qty). Our DB stores
        # per-unit `sale_price`; `get_sales_summary` computes revenue as
        # `SUM(sale_price * quantity)`, so we need per-unit here or revenue
        # inflates for multi-qty orders.
        unit_price = round(line_total / max(qty, 1), 2)
        if subtotal > 0:
            share = line_total / subtotal
        else:
            share = 1.0 / max(len(line_items), 1)
        rows.append(
            {
                "order_number": f"EBAY-{order_num}",
                "order_date": order_date,
                "buyer_name": buyer,
                "status": status,
                "product_name": title,
                "condition": "",
                "quantity": qty,
                "sale_price": unit_price,
                "shipping_amt": round(shipping_total * share, 2),
                "total_amount": round(total * share, 2),
                "fee_amount": round(fees_total * share, 2),
                "net_amount": round((total - fees_total) * share, 2),
                "_sku": sku,
                "_marketplace": "ebay",
            }
        )
    return rows


def _dollars(amount: dict | None) -> float:
    if not amount:
        return 0.0
    try:
        return float(amount.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0
