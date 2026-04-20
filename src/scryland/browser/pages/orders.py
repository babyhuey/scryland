"""Orders page scraping — reads sales data from TCGPlayer."""

from __future__ import annotations

import logging

from playwright.async_api import Page

from scryland.browser.pagination import NextPageResult, click_next_page
from scryland.config import ScrylandConfig
from scryland.exceptions import PaginationIncompleteError

logger = logging.getLogger("scryland")

ORDERS_URL = "https://store.tcgplayer.com/admin/orders"


class OrdersPage:
    """Scrapes order data from TCGPlayer seller portal."""

    def __init__(self, page: Page, config: ScrylandConfig) -> None:
        self._page = page
        self._config = config

    async def navigate(self) -> None:
        """Navigate to the orders page with the 'All' filter applied.

        We set the filter via the URL query string (`qfid=All`) instead of
        clicking a button. This avoids an ambiguity where `text=All` can
        match the cookie banner's "Allow All" button, which then hangs until
        timeout. URL-based filtering is also a full page load — deterministic.
        """
        url = f"{ORDERS_URL}?qfid=All"
        await self._page.goto(url, wait_until="domcontentloaded")
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # Dismiss cookie / consent banners if present — they cover clickable
        # elements further down.
        for label in ("Allow All", "Accept All", "Got it", "Dismiss"):
            try:
                btn = self._page.get_by_role("button", name=label).first
                if await btn.count() > 0:
                    await btn.click(timeout=3000)
                    logger.debug("Dismissed '%s' banner", label)
                    break
            except Exception:
                continue

        logger.info("Orders page loaded (filter: All)")

    async def get_order_rows(self) -> list[dict]:
        """Scrape order summary rows from the orders table, across all pages."""
        await self._page.wait_for_selector("table", timeout=self._config.browser_timeout_ms)
        await self._page.wait_for_timeout(1000)

        all_orders: list[dict] = []
        seen: set[str] = set()
        max_pages = 100  # safety ceiling

        for page_num in range(1, max_pages + 1):
            orders = await self._page.evaluate("""() => {
                const rows = document.querySelectorAll('table tbody tr');
                const results = [];
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 8) continue;

                    const orderLink = cells[1]?.querySelector('a');
                    const orderNumber = orderLink ? orderLink.textContent.trim() : '';
                    const orderHref = orderLink ? orderLink.href : '';

                    results.push({
                        order_number: orderNumber,
                        order_href: orderHref,
                        buyer_name: cells[2]?.innerText.trim() || '',
                        order_date: cells[3]?.innerText.trim() || '',
                        status: cells[4]?.innerText.trim() || '',
                        product_amt: cells[6]?.innerText.trim() || '',
                        shipping_amt: cells[7]?.innerText.trim() || '',
                        total_amount: cells[8]?.innerText.trim() || '',
                    });
                }
                return results;
            }""")

            new_count = 0
            for o in orders:
                key = o.get("order_number", "") or (o.get("order_href") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                all_orders.append(o)
                new_count += 1

            logger.debug("Orders page %d: %d rows (%d new)", page_num, len(orders), new_count)

            nxt = await click_next_page(self._page)
            if nxt is NextPageResult.LAST_PAGE:
                break
            if nxt is NextPageResult.STALLED:
                raise PaginationIncompleteError(f"Orders pagination stalled after page {page_num}")

        logger.debug("Collected %d unique orders across %d pages", len(all_orders), page_num)
        return all_orders

    async def get_order_details(self, order_href: str) -> dict:
        """Navigate to an order detail page and scrape product info.

        Returns dict with: order_number, buyer_name, order_date, status,
        products (list), fee_amount, net_amount, total_amount
        """
        await self._page.goto(order_href, wait_until="domcontentloaded")
        await self._page.wait_for_timeout(2000)

        details = await self._page.evaluate("""() => {
            const text = document.body.innerText;

            // Extract order info from General Info sidebar
            const getField = (label) => {
                const regex = new RegExp(label + '\\s*\\n\\s*(.+)', 'i');
                const match = text.match(regex);
                return match ? match[1].trim() : '';
            };

            let orderNumber = getField('Order Number');
            const status = getField('Status');
            const orderDate = getField('Order Date');

            // Fallback: extract from page title "Order: BFA5A42A-..."
            if (!orderNumber) {
                const titleMatch = text.match(/Order[:\\s]+([A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+)/i);
                if (titleMatch) orderNumber = titleMatch[1];
            }
            // Fallback: extract from URL
            if (!orderNumber) {
                const urlMatch = window.location.href.match(/orders?\\/([^?\\/]+)/i);
                if (urlMatch) orderNumber = urlMatch[1];
            }

            // Extract transaction details
            const feeMatch = text.match(/Fee Amount\\s*\\n?\\s*\\(?\\$([\\d.]+)\\)?/i);
            const netMatch = text.match(/Net Amount\\s*\\n?\\s*\\$([\\d.]+)/i);
            const shippingMatch = text.match(/Shipping Amount\\s*\\n?\\s*\\$([\\d.]+)/i);
            const totalMatch = text.match(/Order Amount\\s*\\n?\\s*\\$([\\d.]+)/i);

            const feeAmount = feeMatch ? parseFloat(feeMatch[1]) : 0;
            const netAmount = netMatch ? parseFloat(netMatch[1]) : 0;
            const shippingAmount = shippingMatch ? parseFloat(shippingMatch[1]) : 0;
            const totalAmount = totalMatch ? parseFloat(totalMatch[1]) : 0;

            // Extract products table
            const products = [];
            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                const header = table.innerText.substring(0, 100);
                if (!header.includes('Product') || !header.includes('Quantity')) continue;

                const rows = table.querySelectorAll('tr');
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 3) continue;

                    const productText = cells[0]?.innerText.trim() || '';
                    if (!productText || productText === 'Subtotal') continue;

                    // Parse product text: "Magic - Set: Card Name - #Number - Condition"
                    const priceText = cells[1]?.innerText.trim() || '0';
                    const qtyText = cells[2]?.innerText.trim() || '1';
                    const extText = cells[3]?.innerText.trim() || '0';

                    products.push({
                        raw_product: productText,
                        sale_price: parseFloat(priceText.replace('$', '').replace(',', '') || '0'),
                        quantity: parseInt(qtyText) || 1,
                        ext_price: parseFloat(extText.replace('$', '').replace(',', '') || '0'),
                    });
                }
            }

            // Extract buyer name
            const buyerMatch = text.match(/Buyer\\s*\\n\\s*(.+)/i);
            const buyerName = buyerMatch ? buyerMatch[1].trim() : '';

            return {
                order_number: orderNumber,
                order_date: orderDate,
                status: status,
                buyer_name: buyerName,
                fee_amount: feeAmount,
                net_amount: netAmount,
                shipping_amt: shippingAmount,
                total_amount: totalAmount,
                products: products,
            };
        }""")

        logger.debug(
            "Order %s: %d products, total $%.2f, net $%.2f",
            details.get("order_number", "?"),
            len(details.get("products", [])),
            details.get("total_amount", 0),
            details.get("net_amount", 0),
        )
        return details

    async def go_back_to_orders(self) -> None:
        """Navigate back to the orders list."""
        back_link = await self._page.query_selector("text=Back to Orders")
        if back_link:
            await back_link.click()
        else:
            await self._page.goto(ORDERS_URL)
        await self._page.wait_for_load_state("domcontentloaded")
        await self._page.wait_for_timeout(1000)


def parse_product_name(raw: str) -> tuple[str, str]:
    """Parse a TCGPlayer order product string into (card_name, condition).

    Input format: "Magic - Secret Lair Drop Series: Shredder, Criminal Mastermind - Higure, the Still Wind - #2368 - Near Mint"
    Returns: ("Shredder, Criminal Mastermind - Higure, the Still Wind", "Near Mint")
    """
    # Split by " - " and work backwards
    parts = raw.split(" - ")

    # Last part is usually the condition
    condition = ""
    if parts and parts[-1].strip() in (
        "Near Mint",
        "Lightly Played",
        "Moderately Played",
        "Heavily Played",
        "Damaged",
        "Near Mint Foil",
        "Lightly Played Foil",
        "Moderately Played Foil",
        "Heavily Played Foil",
        "Damaged Foil",
    ):
        condition = parts.pop().strip()

    # Remove collector number (starts with #)
    if parts and parts[-1].strip().startswith("#"):
        parts.pop()

    # Remove the "Magic - Set:" prefix
    # First part is usually "Magic" or the game name
    if parts and ":" in parts[0]:
        # "Magic - Secret Lair Drop Series: Card Name"
        first = parts[0]
        colon_idx = first.index(":")
        parts[0] = first[colon_idx + 1 :].strip()
    elif len(parts) > 1:
        # First part is just "Magic", second might have "Set: Card"
        parts.pop(0)
        if parts and ":" in parts[0]:
            first = parts[0]
            colon_idx = first.index(":")
            parts[0] = first[colon_idx + 1 :].strip()

    card_name = " - ".join(parts).strip()
    return card_name, condition
