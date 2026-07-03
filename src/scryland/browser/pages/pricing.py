"""Pricing page interactions — editing prices on the manage page."""

from __future__ import annotations

import logging

from playwright.async_api import Page

from scryland.config import ScrylandConfig
from scryland.exceptions import SelectorNotFoundError
from scryland.models import PriceUpdate

logger = logging.getLogger("scryland")


class PricingPage:
    """Handles price editing on TCGPlayer product manage pages."""

    def __init__(self, page: Page, config: ScrylandConfig) -> None:
        self._page = page
        self._config = config

    async def _find_pricing_table(self, poll_ms: int = 10000):
        """Find the pricing table on the manage page.

        The table can render slightly after DOMContentLoaded, so we poll
        for up to `poll_ms` milliseconds before giving up.
        """
        import asyncio

        deadline = poll_ms / 1000.0
        start = asyncio.get_running_loop().time()
        while True:
            all_tables = await self._page.query_selector_all("table")
            for table in all_tables:
                header_text = await table.evaluate("el => el.innerText.substring(0, 200)")
                if "Condition" in header_text and "TCG" in header_text:
                    return table
            if asyncio.get_running_loop().time() - start >= deadline:
                return None
            await asyncio.sleep(0.25)

    async def _find_condition_row(self, condition: str):
        """Find the table row matching a specific condition."""
        pricing_table = await self._find_pricing_table()
        if not pricing_table:
            # Pricing table never rendered — flaky/timing, worth a retry.
            raise SelectorNotFoundError("Pricing table not rendered")

        rows = await pricing_table.query_selector_all("tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if cells:
                cell_text = (await cells[0].inner_text()).strip()
                if cell_text == condition:
                    return row
        return None

    async def apply_price_update(self, update: PriceUpdate) -> None:
        """Update a single listing's price on the manage page.

        Prefers clicking the Match button for TCG Lowest Listing.
        Falls back to manually typing the price.
        """
        logger.info(
            "Updating '%s' (%s): $%.2f → $%.2f",
            update.listing.product_name,
            update.listing.condition,
            update.old_price,
            update.new_price,
        )

        row = await self._find_condition_row(update.listing.condition)
        if row is None:
            raise SelectorNotFoundError(
                f"Could not find row for condition '{update.listing.condition}'"
            )

        # Get the price input field for this row
        price_input = await row.evaluate_handle("""el => {
            const inputs = el.querySelectorAll('input[type="text"], input[type="number"]');
            for (const inp of inputs) {
                if (inp.value !== 'Match' && inp.type !== 'submit') return inp;
            }
            return null;
        }""")
        price_el = price_input.as_element()
        if not price_el:
            raise SelectorNotFoundError("Price input field not found")

        # Read value before change
        val_before = await price_el.input_value()
        logger.debug("Price input value before: '%s'", val_before)

        # Try clicking the Match button for TCG Lowest Listing
        if update.new_price == update.listing.tcg_low_price:
            # Try multiple approaches to click the Match button
            match_clicked = await row.evaluate("""el => {
                const buttons = el.querySelectorAll('input[value="Match"]');
                if (buttons.length === 0) return false;
                const btn = buttons[0];
                // Try native click
                btn.click();
                // Also try dispatching events that Knockout.js may listen for
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return true;
            }""")

            if match_clicked:
                await self._page.wait_for_timeout(1500)

                val_after = await price_el.input_value()
                logger.debug("Price input value after Match click: '%s'", val_after)
                if val_after and val_after != val_before:
                    logger.info(
                        "Match button updated price: '%s' → '%s'",
                        val_before,
                        val_after,
                    )
                    return
                else:
                    # Try Playwright's click as fallback (handles iframes, overlays)
                    try:
                        match_handle = await row.evaluate_handle("""el => {
                            const buttons = el.querySelectorAll('input[value="Match"]');
                            return buttons.length > 0 ? buttons[0] : null;
                        }""")
                        btn_el = match_handle.as_element()
                        if btn_el:
                            await btn_el.click(force=True)
                            await self._page.wait_for_timeout(1500)
                            val_after = await price_el.input_value()
                            if val_after and val_after != val_before:
                                logger.info(
                                    "Match button (force click) updated price: '%s' → '%s'",
                                    val_before,
                                    val_after,
                                )
                                return
                    except Exception:
                        pass

                    logger.warning(
                        "Match button did not change price (still '%s'), "
                        "falling back to manual entry",
                        val_after,
                    )

        # Fallback: manually type the price into the input field
        await self._page.wait_for_timeout(500)
        await price_el.click(click_count=3)  # Select all
        await price_el.fill(f"{update.new_price:.2f}")
        # Verify it took
        val_after = await price_el.input_value()
        logger.debug("Price input value after manual fill: '%s'", val_after)
        await self._page.wait_for_timeout(300)
        await price_el.press("Tab")  # Confirm edit
        logger.info("Manually set price to $%.2f", update.new_price)

    async def set_quantity_zero(self, condition: str) -> None:
        """Set quantity to 0 for a condition row (effectively delisting it)."""
        row = await self._find_condition_row(condition)
        if row is None:
            raise SelectorNotFoundError(f"Condition '{condition}' not found")

        qty_handle = await row.evaluate_handle("""el => {
            const inputs = el.querySelectorAll('input[type="text"], input[type="number"]');
            let found = 0;
            for (const inp of inputs) {
                if (inp.value === 'Match' || inp.type === 'submit') continue;
                found++;
                if (found === 2) return inp;  // Second input = quantity
            }
            return null;
        }""")
        qty_el = qty_handle.as_element()
        if qty_el:
            await qty_el.click(click_count=3)
            await qty_el.fill("0")
            await self._page.wait_for_timeout(300)
            logger.info("Set quantity to 0 for '%s'", condition)
        else:
            raise SelectorNotFoundError(f"Quantity input not found for '{condition}'")

    async def apply_match_lowest(self, condition: str) -> None:
        """Find a condition row and click its TCG Lowest Match button."""
        row = await self._find_condition_row(condition)
        if row is None:
            raise SelectorNotFoundError(f"Condition '{condition}' not found")

        # Click the first Match button (TCG Lowest)
        clicked = await row.evaluate("""el => {
            const buttons = el.querySelectorAll('input[value="Match"]');
            if (buttons.length === 0) return false;
            buttons[0].click();
            buttons[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            return true;
        }""")

        if clicked:
            await self._page.wait_for_timeout(1500)
            logger.info("Clicked TCG Lowest Match for '%s'", condition)
        else:
            raise SelectorNotFoundError(f"No Match button found for '{condition}'")

    async def save_changes(self) -> None:
        """Click the Save button on the manage page."""
        # The Save button is a link/button at the top right of the manage page
        # Try multiple selectors
        save_selectors = [
            "input[value='Save']",
            "a:has-text('Save')",
            "button:has-text('Save')",
        ]
        for selector in save_selectors:
            save_btn = self._page.locator(selector).first
            if await save_btn.count() > 0:
                # Log what we found
                tag = await save_btn.evaluate("el => el.tagName")
                href = await save_btn.evaluate("el => el.href || ''")
                logger.debug("Found Save button: <%s> href='%s'", tag, href)

                await save_btn.click()
                await self._page.wait_for_load_state("networkidle")
                await self._page.wait_for_timeout(1000)
                logger.info("Changes saved (clicked %s)", tag)
                return

        raise SelectorNotFoundError("Save button not found on manage page")
