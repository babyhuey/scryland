"""Price Differential Report page — fast way to find cards needing price updates."""

from __future__ import annotations

import logging

from playwright.async_api import Page

from scryland.browser.pagination import NextPageResult, click_next_page
from scryland.config import ScrylandConfig
from scryland.exceptions import PaginationIncompleteError

logger = logging.getLogger("scryland")

REPORT_URL = "https://store.tcgplayer.com/admin/report/pricedifferential"


def _dedupe_new_rows(rows: list[dict], seen: set[tuple[str, str, str]]) -> list[dict]:
    """Filter `rows` down to ones not already in `seen`, updating `seen` in place.

    Keyed on (product_name, set_name, condition) — set name is required
    because two different printings of the same card/condition (e.g. two
    "Reprieve" printings from different sets) share (product_name,
    condition) and would otherwise collide, silently dropping the second
    printing's price differential.
    """
    new_rows = []
    for r in rows:
        key = (r["product_name"], r["set_name"], r["condition"])
        if key in seen:
            continue
        seen.add(key)
        new_rows.append(r)
    return new_rows


class PriceReportPage:
    """Scrapes the Price Differential Report to find cards needing updates."""

    def __init__(self, page: Page, config: ScrylandConfig) -> None:
        self._page = page
        self._config = config

    async def navigate(self) -> None:
        """Navigate to the price differential report and run it."""
        await self._page.goto(REPORT_URL, wait_until="domcontentloaded")
        await self._page.wait_for_timeout(2000)

        # Make sure "Lowest Listing" is selected (should be default)
        # Click Search to run the report
        search_btn = self._page.locator("input[value='Search'], button:has-text('Search')").first
        await search_btn.click()
        await self._page.wait_for_load_state("networkidle")
        await self._page.wait_for_timeout(1000)

        logger.info("Price differential report loaded")

    async def get_differentials(self) -> list[dict]:
        """Scrape all rows from the report results, across all pages."""
        all_results: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        max_pages = 100

        for page_num in range(1, max_pages + 1):
            results = await self._page.evaluate("""() => {
                const rows = document.querySelectorAll('table tbody tr');
                const results = [];
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 8) continue;

                    const productName = cells[0]?.innerText.trim() || '';
                    if (!productName || productName === 'Product Name') continue;

                    const parsePrice = (text) => {
                        const match = text.match(/\\$(\\d+[\\d,.]*)/);
                        return match ? parseFloat(match[1].replace(',', '')) : 0;
                    };

                    results.push({
                        product_name: productName,
                        set_name: cells[1]?.innerText.trim() || '',
                        condition: cells[2]?.innerText.trim() || '',
                        quantity: parseInt(cells[3]?.innerText.trim() || '0') || 0,
                        marketplace_price: parsePrice(cells[4]?.innerText || ''),
                        lowest_listing: parsePrice(cells[5]?.innerText || ''),
                        pct_differential: cells[6]?.innerText.trim() || '',
                        dollar_differential: cells[7]?.innerText.trim() || '',
                    });
                }
                return results;
            }""")

            new_rows = _dedupe_new_rows(results, seen)
            all_results.extend(new_rows)

            logger.debug(
                "Report page %d: %d rows (%d new)", page_num, len(results), len(new_rows)
            )

            nxt = await click_next_page(self._page)
            if nxt is NextPageResult.LAST_PAGE:
                break
            if nxt is NextPageResult.STALLED:
                raise PaginationIncompleteError(
                    f"Price report pagination stalled after page {page_num}"
                )

        logger.info(
            "Found %d items with price differentials across %d page(s)", len(all_results), page_num
        )
        return all_results

    async def click_manage_for_row(self, product_name: str) -> bool:
        """Click the Manage button for a product, paginating until found.

        Matches the row by exact equality on the name cell (cells[0], per
        get_differentials' column mapping) — a substring match let a target
        like "Reprieve" match "Reprieve's Haunt" instead. If the matching
        row has no control whose text/value is exactly "Manage", we refuse
        to click anything in that row rather than grabbing the first
        anchor/button/input (which could be the product name link).
        """
        max_pages = 100
        for _ in range(max_pages):
            clicked = await self._page.evaluate(
                """(targetName) => {
                const rows = document.querySelectorAll('table tbody tr');
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    const name = (cells[0] ? cells[0].innerText : '').trim();
                    if (name !== targetName) continue;
                    const els = row.querySelectorAll('a, button, input');
                    for (const el of els) {
                        const text = (el.textContent || el.value || '').trim();
                        if (text === 'Manage') { el.click(); return true; }
                    }
                    return false;
                }
                return false;
            }""",
                product_name,
            )
            if clicked:
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await self._page.wait_for_timeout(1000)
                return True

            nxt = await click_next_page(self._page)
            if nxt is NextPageResult.LAST_PAGE:
                return False
            if nxt is NextPageResult.STALLED:
                raise PaginationIncompleteError(
                    f"Price report pagination stalled while searching for '{product_name}'"
                )
        return False

    async def go_back_to_report(self) -> None:
        """Navigate back to the report page."""
        from scryland.browser.flaky import retry_on_flaky

        await retry_on_flaky(
            lambda: self._page.goto(REPORT_URL, wait_until="domcontentloaded"),
            page=self._page,
            label="go_back_to_report goto",
        )
        await self._page.wait_for_timeout(1000)
        # Re-run the search
        search_btn = self._page.locator("input[value='Search'], button:has-text('Search')").first
        await search_btn.click()
        await self._page.wait_for_load_state("networkidle")
        await self._page.wait_for_timeout(1000)
