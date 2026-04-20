"""Inventory page scraping and navigation to manage pages."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from playwright.async_api import Page

from scryland.browser.flaky import retry_on_flaky
from scryland.browser.pagination import NextPageResult, click_next_page
from scryland.browser.selectors import Selectors
from scryland.config import ScrylandConfig
from scryland.exceptions import NavigationError, PaginationIncompleteError, SelectorNotFoundError
from scryland.models import Listing

logger = logging.getLogger("scryland")


def _parse_price(text: str | None) -> Decimal | None:
    """Parse a price string like '$20.34' or '$20.34 + Shipping: $0.99' into a Decimal.

    Only parses the base price, ignoring shipping.
    Returns None if the text is empty, missing, or unparseable.
    """
    if not text:
        return None
    # Take only the first price (before "+ Shipping")
    cleaned = text.split("+")[0].strip()
    cleaned = cleaned.replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned == "-" or cleaned.lower() == "n/a":
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        logger.warning("Could not parse price: '%s'", text)
        return None


class InventoryPage:
    """Navigates the inventory catalog and manages individual product pages."""

    def __init__(self, page: Page, config: ScrylandConfig) -> None:
        self._page = page
        self._config = config

    async def navigate(self) -> None:
        """Navigate to the inventory catalog page with My Inventory Only checked."""
        logger.info("Navigating to inventory page...")
        await self._page.goto(
            self._config.inventory_url,
            wait_until="networkidle",
        )

        # Wait for the catalog table to appear
        try:
            await self._page.wait_for_selector(
                Selectors.CATALOG_TABLE,
                timeout=self._config.browser_timeout_ms,
            )
        except Exception as exc:
            raise NavigationError(
                "Inventory table not found. Selectors may need updating — "
                "use `scryland explore` to inspect the page."
            ) from exc

        await self._apply_my_inventory_filter()

        logger.info("Inventory page loaded")

    async def _apply_my_inventory_filter(self) -> None:
        """Ensure 'My Inventory Only' is checked and click Search."""
        try:
            # Find the actual checkbox input (not the label)
            checkbox = await self._page.query_selector("input[type='checkbox']")
            if checkbox:
                is_checked = await checkbox.is_checked()
                if not is_checked:
                    # Click the label to check it
                    checkbox_label = self._page.locator("text=My Inventory Only")
                    if await checkbox_label.count() > 0:
                        await checkbox_label.click()
                        logger.debug("Checked 'My Inventory Only'")
                        await self._page.wait_for_timeout(500)
                else:
                    logger.debug("'My Inventory Only' already checked")

            search_btn = self._page.locator(Selectors.SEARCH_BUTTON)
            await search_btn.click()
            await self._page.wait_for_load_state("networkidle")
            # Wait for table to re-render with filtered results
            await self._page.wait_for_timeout(1000)
            logger.info("Filtered to 'My Inventory Only'")
        except Exception:
            logger.warning("Could not apply 'My Inventory Only' filter", exc_info=True)

    async def get_product_names(self) -> list[dict[str, str]]:
        """Get product names for rows with a Manage button, across all pages.

        Raises PaginationIncompleteError if any Next click stalls — callers
        should not treat a partial list as authoritative.
        """
        all_products: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        max_pages = 100

        for page_num in range(1, max_pages + 1):
            products = await self._page.evaluate("""() => {
                const results = [];
                const allElements = document.querySelectorAll('a, button, input');
                for (const el of allElements) {
                    const text = (el.textContent || el.value || '').trim();
                    if (text !== 'Manage') continue;
                    const row = el.closest('tr');
                    if (!row) continue;
                    const cells = row.querySelectorAll('td');
                    const name = cells.length >= 3 ? cells[2].innerText.trim() : '';
                    const set = cells.length >= 4 ? cells[3].innerText.trim() : '';
                    if (name) results.push({name: name, set: set});
                }
                return results;
            }""")

            new_count = 0
            for p in products:
                key = (p["name"], p["set"])
                if key in seen:
                    continue
                seen.add(key)
                all_products.append(p)
                new_count += 1

            logger.debug("Page %d: %d products (%d new)", page_num, len(products), new_count)

            nxt = await click_next_page(self._page)
            if nxt is NextPageResult.LAST_PAGE:
                break
            if nxt is NextPageResult.STALLED:
                raise PaginationIncompleteError(
                    f"Inventory pagination stalled after page {page_num}"
                )

        logger.debug("Collected %d unique products across %d pages", len(all_products), page_num)
        return all_products

    async def click_manage_for_product(self, product_name: str) -> None:
        """Click the Manage button for a product, paginating until found."""
        max_pages = 100
        for _ in range(max_pages):
            clicked = await retry_on_flaky(
                lambda: self._page.evaluate(
                    """(targetName) => {
                    const elements = document.querySelectorAll('a, button, input');
                    for (const el of elements) {
                        const text = (el.textContent || el.value || '').trim();
                        if (text !== 'Manage') continue;
                        const row = el.closest('tr');
                        if (!row) continue;
                        const cells = row.querySelectorAll('td');
                        const name = cells.length >= 3 ? cells[2].innerText.trim() : '';
                        if (name === targetName) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                    product_name,
                ),
                page=self._page,
                label=f"click_manage_for_product({product_name!r})",
            )
            if clicked:
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                return

            nxt = await click_next_page(self._page)
            if nxt is NextPageResult.LAST_PAGE:
                break
            if nxt is NextPageResult.STALLED:
                raise PaginationIncompleteError(
                    f"Inventory pagination stalled while searching for '{product_name}'"
                )

        raise SelectorNotFoundError(f"Product '{product_name}' not found in inventory")

    async def go_back_to_inventory(self, reapply_filter: bool = True) -> None:
        """Go back to inventory list.

        Args:
            reapply_filter: If True, re-applies My Inventory Only + Search.
                          Set to False if the filter is already sticky.
        """
        back_link = await self._page.query_selector(Selectors.BACK_TO_INVENTORY)
        if back_link:
            await back_link.click()
        else:
            await self._page.goto(self._config.inventory_url)

        # Wait for the new page to fully load before interacting
        await self._page.wait_for_load_state("domcontentloaded")
        await self._page.wait_for_timeout(1000)

        if reapply_filter:
            await self._apply_my_inventory_filter()

    async def get_manage_page_listings(self, product_name: str) -> list[Listing]:
        """Scrape all condition rows from the manage page for a product.

        Returns only rows that have a quantity > 0 (i.e., actively listed).
        """
        # Wait for the pricing table
        try:
            # There may be multiple tables; the pricing table is the one with conditions
            await self._page.wait_for_selector(
                Selectors.PRICING_TABLE,
                timeout=self._config.browser_timeout_ms,
            )
        except Exception as exc:
            raise NavigationError("Pricing table not found on manage page") from exc

        # Check "If me, show next lowest" so we see the next lowest price
        # when we're already the lowest seller
        try:
            show_next = self._page.locator("text=If me, show next lowest")
            if await show_next.count() > 0:
                # Find the checkbox near this text
                checkbox = self._page.locator(
                    "text=If me, show next lowest >> input[type='checkbox']"
                )
                if await checkbox.count() == 0:
                    # Try clicking the label itself
                    await show_next.click()
                else:
                    if not await checkbox.is_checked():
                        await checkbox.click()
                await self._page.wait_for_timeout(1000)
                logger.debug("Checked 'If me, show next lowest'")
        except Exception:
            logger.debug("Could not check 'If me, show next lowest'", exc_info=True)

        # The pricing table has a header row with "Condition" as the first column.
        # Find that specific table among multiple tables on the page.
        pricing_table = None
        all_tables = await self._page.query_selector_all("table")
        for table in all_tables:
            header_text = await table.evaluate("el => el.innerText.substring(0, 200)")
            if "Condition" in header_text and "TCG" in header_text:
                pricing_table = table
                break

        listings: list[Listing] = []

        if not pricing_table:
            logger.warning("Could not find pricing table on manage page")
            return listings

        rows = await pricing_table.query_selector_all("tr")
        logger.debug("Found %d rows in pricing table", len(rows))

        # Log first few data rows for debugging
        for i in range(min(4, len(rows))):
            text = await rows[i].inner_text()
            logger.debug("Pricing row %d: %s", i, text[:200])

        parse_errors = 0
        for i, row in enumerate(rows):
            try:
                listing = await self._parse_manage_row(row, product_name)
                if listing:
                    logger.debug(
                        "Row %d: condition='%s', price=%s, qty=%d",
                        i,
                        listing.condition,
                        listing.current_price,
                        listing.quantity,
                    )
                    if listing.quantity > 0:
                        listings.append(listing)
            except Exception:
                parse_errors += 1
                logger.debug("Unparseable row %d", i, exc_info=True)

        # If we had real rows but produced no listings, the parser likely
        # drifted from TCG's markup — surface it instead of returning [].
        data_rows = max(len(rows) - 1, 0)  # subtract header
        if data_rows >= 3 and not listings and parse_errors:
            logger.warning(
                "get_manage_page_listings: %d rows scanned, %d parse errors, "
                "0 listings extracted for '%s' — pricing-table layout may have changed",
                data_rows,
                parse_errors,
                product_name,
            )
        logger.info("Found %d active listings for '%s'", len(listings), product_name)
        return listings

    async def _parse_manage_row(self, row, product_name: str) -> Listing | None:
        """Parse a single row from the manage page pricing table.

        Uses JavaScript to extract data since the column layout varies
        and has Match buttons interspersed between data columns.
        """
        # Use JS to extract all data from the row at once
        data = await row.evaluate("""el => {
            const cells = el.querySelectorAll('td');
            if (cells.length < 5) return null;

            // Find all input fields in the row
            const inputs = el.querySelectorAll('input[type="text"], input[type="number"]');
            let priceInput = null;
            let qtyInput = null;

            // The price input is typically the first text input, qty is the last
            for (const inp of inputs) {
                const val = inp.value;
                // Skip Match buttons
                if (inp.type === 'submit' || inp.value === 'Match') continue;
                if (!priceInput) {
                    priceInput = inp;
                } else {
                    qtyInput = inp;
                }
            }

            // Get condition from first cell
            const condition = cells[0] ? cells[0].innerText.trim() : '';

            // Get TCG Lowest from second cell (text only, not Match button)
            const tcgLowest = cells[1] ? cells[1].innerText.trim() : '';

            // Get TCG Last Sold
            const tcgLastSold = cells[3] ? cells[3].innerText.trim() : '';

            // Get TCG Market Price
            const tcgMarket = cells[5] ? cells[5].innerText.trim() : '';

            return {
                condition: condition,
                tcgLowest: tcgLowest,
                tcgLastSold: tcgLastSold,
                tcgMarket: tcgMarket,
                price: priceInput ? priceInput.value : '',
                quantity: qtyInput ? qtyInput.value : '0',
            };
        }""")

        if not data or not data.get("condition"):
            return None

        condition = data["condition"]
        # Skip header rows, empty rows, or checkbox rows
        if condition in ("Condition", "") or "If me" in condition or "show next" in condition:
            return None

        quantity = 0
        try:
            quantity = int(data.get("quantity", "0") or "0")
        except ValueError:
            pass

        current_price = _parse_price(data.get("price"))
        tcg_low_price = _parse_price(data.get("tcgLowest"))
        market_price = _parse_price(data.get("tcgMarket"))
        tcg_last_sold = _parse_price(data.get("tcgLastSold"))

        # If no current price set, still include it — we may want to set one
        if current_price is None and quantity > 0:
            current_price = Decimal("0")

        if current_price is None:
            return None

        return Listing(
            product_name=product_name,
            condition=condition,
            quantity=quantity,
            current_price=current_price,
            tcg_low_price=tcg_low_price,
            tcg_last_sold=tcg_last_sold,
            market_price=market_price,
        )
