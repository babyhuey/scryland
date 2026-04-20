"""Pricing engine — orchestrates the full optimization flow."""

from __future__ import annotations

import logging

from scryland.browser.pages.inventory import InventoryPage
from scryland.browser.pages.pricing import PricingPage
from scryland.browser.session import BrowserSession
from scryland.config import ScrylandConfig
from scryland.models import Listing, PriceUpdate, PricingReport, UpdateStatus
from scryland.pricing.comparator import PriceComparator
from scryland.pricing.guardrails import PriceGuardrails

logger = logging.getLogger("scryland")


class PricingEngine:
    """Core orchestrator: for each product, scrape → compare → guardrail → apply.

    Flow:
    1. Navigate to inventory (My Inventory Only)
    2. For each product, click Manage
    3. On the manage page, scrape condition rows with quantity > 0
    4. Compute optimal prices via comparator
    5. Apply guardrails (flag large changes)
    6. Prompt for confirmation on flagged changes
    7. Apply approved changes (unless dry-run)
    8. Save and go back to inventory
    """

    def __init__(
        self,
        config: ScrylandConfig,
        comparator: PriceComparator,
        guardrails: PriceGuardrails,
    ) -> None:
        self._config = config
        self._comparator = comparator
        self._guardrails = guardrails

    async def run(self, session: BrowserSession) -> tuple[PricingReport, list[Listing]]:
        """Run the full price optimization flow.

        Returns (report, all_listings) so callers can reuse the scraped data.
        """
        report = PricingReport(dry_run=self._config.dry_run)
        all_scraped_listings: list[Listing] = []
        inventory_page = InventoryPage(session.page, self._config)
        pricing_page = PricingPage(session.page, self._config)

        # Navigate to inventory
        await inventory_page.navigate()

        # Get list of products
        products = await inventory_page.get_product_names()
        logger.info("Found %d products in inventory", len(products))

        if not products:
            logger.info("No products found in inventory")
            return report, all_scraped_listings

        # Process each product
        for idx, product in enumerate(products):
            product_name = product["name"]
            logger.info(
                "Processing product %d/%d: %s",
                idx + 1,
                len(products),
                product_name,
            )

            # Human-like delay between products
            await session.human_delay()
            await session.dismiss_popups()

            try:
                # Click Manage for this product
                await inventory_page.click_manage_for_product(product_name)
            except Exception:
                # Retry once — re-apply filter in case the page went stale
                logger.debug("Manage button not found for '%s', retrying with filter", product_name)
                try:
                    await inventory_page.go_back_to_inventory(reapply_filter=True)
                    await session.human_delay()
                    await inventory_page.click_manage_for_product(product_name)
                except Exception:
                    logger.warning(
                        "Could not open manage page for '%s', skipping",
                        product_name,
                    )
                    continue
            await session.dismiss_popups()

            # Scrape listings from the manage page
            try:
                listings = await inventory_page.get_manage_page_listings(product_name)
                all_scraped_listings.extend(listings)
            except Exception:
                logger.warning(
                    "Could not scrape listings for '%s', skipping",
                    product_name,
                    exc_info=True,
                )
                await session.human_delay()
                await inventory_page.go_back_to_inventory(reapply_filter=False)
                continue
            report.total_listings += len(listings)

            # Process each listing (condition row with quantity > 0)
            product_updates: list[PriceUpdate] = []
            for listing in listings:
                update = self._process_listing(listing)
                if update:
                    product_updates.append(update)

            if product_updates:
                # Apply guardrails
                product_updates = self._guardrails.validate_batch(product_updates)
                report.updates_proposed += len(product_updates)

                # Handle each update
                for update in product_updates:
                    await session.human_delay()
                    await self._handle_update(update, pricing_page, report)

                # Save changes for this product if any were applied
                saved = False
                if not self._config.dry_run and any(
                    u.status == UpdateStatus.APPLIED for u in product_updates
                ):
                    await session.human_delay()
                    try:
                        await pricing_page.save_changes()
                        saved = True
                    except Exception:
                        logger.exception("Failed to save changes for '%s'", product_name)
            else:
                saved = False
                logger.info("All prices optimal for '%s'", product_name)

            # Go back to inventory for next product
            # Re-apply filter after saves (Save reloads the page and clears the filter)
            await session.human_delay()
            await inventory_page.go_back_to_inventory(reapply_filter=saved)

        return report, all_scraped_listings

    async def _handle_update(
        self,
        update: PriceUpdate,
        pricing_page: PricingPage,
        report: PricingReport,
    ) -> None:
        """Handle a single update: confirm if needed, apply or skip."""
        if update.status == UpdateStatus.REJECTED:
            report.updates_rejected += 1
            report.updates.append(update)
            return

        # Prompt for confirmation if needed
        if update.requires_confirmation:
            if self._guardrails.prompt_confirmation(update):
                update.approve()
            else:
                update.reject()
                report.updates_rejected += 1
                report.updates.append(update)
                return
        else:
            update.approve()

        # Apply the change
        if self._config.dry_run:
            logger.info(
                "DRY RUN: Would update '%s' (%s) from $%.2f to $%.2f",
                update.listing.product_name,
                update.listing.condition,
                update.old_price,
                update.new_price,
            )
            report.updates_skipped += 1
        else:
            try:
                await pricing_page.apply_price_update(update)
                update.mark_applied()
                report.updates_applied += 1
            except Exception:
                logger.exception(
                    "Failed to apply price update for '%s' (%s)",
                    update.listing.product_name,
                    update.listing.condition,
                )
                update.mark_failed()
                report.updates_failed += 1
        report.updates.append(update)

    def _process_listing(self, listing: Listing) -> PriceUpdate | None:
        """Compute an update for a single listing, if needed."""
        optimal = self._comparator.compute_optimal_price(listing)
        if optimal is None:
            return None

        change_pct = self._comparator.compute_change_pct(listing.current_price, optimal)

        return PriceUpdate(
            listing=listing,
            new_price=optimal,
            old_price=listing.current_price,
            change_pct=change_pct,
        )
