"""CLI interface for Scryland."""

import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from scryland.config import ScrylandConfig
from scryland.models import PricingReport
from scryland.safe_logging import setup_logging

console = Console()
logger = logging.getLogger("scryland")


def _ebay_passphrase(config: "ScrylandConfig") -> str:
    """Resolve the eBay credential passphrase — env/.env first, prompt fallback."""
    if config.ebay_passphrase:
        return config.ebay_passphrase
    from rich.prompt import Prompt

    return Prompt.ask("Passphrase (to decrypt eBay credentials)", password=True)


def _empty_ebay_result(*, error: str | None = None) -> dict:
    """Zeroed result dict so callers can safely do `result.get(...)` etc."""
    return {
        "new_sales": 0,
        "updated": 0,
        "delisted": 0,
        "withdrawn": 0,
        "checked": 0,
        "changes": [],
        "delisted_items": [],
        "tcg_delist_failed": 0,
        "browse_errors": 0,
        "skipped_big_drops": 0,
        "withdraw_failed": 0,
        "update_failed": 0,
        "error": error,
    }


async def _fast_update_ebay_price(
    ebay_client,
    db,
    card,
    existing: dict,
    undercut: bool,
    min_price: float,
    console,
) -> bool:
    """Update an already-listed eBay offer's price without republishing.

    Skips Scryfall lookup, inventory_item PUT, and publish — ~5x faster than
    the full list-on-ebay pipeline. Also upserts the new price into the local
    ebay_listings table. Use when card data hasn't changed, only the price.
    """
    import logging

    logger = logging.getLogger("scryland")

    price = float(card.effective_price)
    if undercut:
        browse_error = None
        try:
            lowest = await ebay_client.find_lowest_price(
                card.card_name,
                card.set_name,
                card.collector_number,
                card.is_foil,
                condition=card.tcg_condition,
                include_foil=card.is_foil,
            )
        except Exception as exc:
            lowest = None
            browse_error = exc
        if lowest is None:
            if browse_error is not None:
                logger.warning(
                    "Browse lookup failed for %s: %s — keeping CSV price",
                    card.card_name,
                    browse_error,
                )
                console.print(
                    f"  [yellow]Browse query failed — keeping CSV "
                    f"${price:.2f} (undercut skipped)[/yellow]"
                )
            else:
                console.print(f"  [dim]No eBay matches — keeping CSV ${price:.2f}[/dim]")
        else:
            # Total-price undercut.
            our_ship = getattr(ebay_client._config, "ebay_shipping_cost", 0.99)
            target_total = lowest - 0.01
            undercut_target = round(target_total - our_ship, 2)
            # Respect BOTH the eBay hard $0.99 floor AND user's min_price.
            price = max(undercut_target, min_price, 0.99)
            if undercut_target < max(min_price, 0.99):
                our_total = price + our_ship
                console.print(
                    f"  [dim]Match: competitor total ${lowest:.2f}; "
                    f"our total ${our_total:.2f} at ${price:.2f} + "
                    f"${our_ship:.2f} ship[/dim]"
                )
            else:
                console.print(
                    f"  [dim]Undercut: competitor total ${lowest:.2f} → "
                    f"ours ${price + our_ship:.2f} (${price:.2f} + "
                    f"${our_ship:.2f} ship)[/dim]"
                )
    if price < 0.99:
        price = 0.99

    current_price = existing.get("price") or 0
    if abs(price - current_price) < 0.01:
        console.print(f"  [dim]Price unchanged at ${current_price:.2f} — skip[/dim]")
        return True

    ok = await ebay_client.update_offer_price(
        existing["offer_id"],
        price,
        existing["quantity"],
    )
    if not ok:
        console.print("  [red]Fast update failed — try --full-republish[/red]")
        return False

    db.upsert_ebay_listing(
        sku=existing["sku"],
        offer_id=existing["offer_id"],
        listing_id=existing.get("listing_id"),
        product_name=existing["product_name"],
        set_name=existing["set_name"],
        collector_number=existing["collector_number"],
        condition=existing["condition"],
        is_foil=bool(existing["is_foil"]),
        price=price,
        quantity=existing["quantity"],
        status="active",
    )
    console.print(
        f"  [green]Updated ${current_price:.2f} → ${price:.2f}[/green] "
        f"[dim](fast path — no republish)[/dim]"
    )
    return True


async def _end_tcg_listing_by_canonical(session, config, db, canonical_key: str) -> bool:
    """End a TCG listing matching `canonical_key` by setting qty=0 and saving.

    Requires an already-started browser session. Returns True on success.
    Wraps the per-step calls in retry_on_flaky so mid-navigation races
    (execution-context-destroyed / stale-handle) don't tank the delist.
    """
    from scryland.browser.flaky import retry_on_flaky
    from scryland.browser.pages.inventory import InventoryPage
    from scryland.browser.pages.pricing import PricingPage

    if session is None:
        return False
    row = db.find_inventory_by_canonical(canonical_key)
    if not row:
        return False
    inventory_page = InventoryPage(session.page, config)
    pricing_page = PricingPage(session.page, config)
    try:
        await retry_on_flaky(
            lambda: inventory_page.navigate(),
            page=session.page,
            label="delist: inventory.navigate",
        )
        from scryland.exceptions import SelectorNotFoundError

        try:
            await retry_on_flaky(
                lambda: inventory_page.click_manage_for_product(row["product_name"]),
                page=session.page,
                label="delist: click_manage",
            )
        except SelectorNotFoundError:
            # Matcher couldn't locate a Manage row under any of its three
            # tiers. Before marking the local row removed, require a
            # positive-absence signal — use the inventory search box to
            # confirm zero Manage buttons match the name. A false "absent"
            # conclusion here silently leaves the TCG listing live and has
            # caused cross-sell incidents in the past, so be conservative:
            # only trust absence when verified.
            verified_absent = await inventory_page.verify_product_absent(row["product_name"])
            if verified_absent:
                console.print(
                    f"  [dim]TCG listing for '{row['product_name']}' verified "
                    f"absent — marking reconciled.[/dim]"
                )
                db.conn.execute(
                    "UPDATE inventory SET status = 'removed' WHERE id = ?",
                    (row["id"],),
                )
                db.conn.commit()
                return True
            console.print(
                f"  [red]TCG delist: couldn't find Manage row for "
                f"'{row['product_name']}' and absence UNVERIFIED — listing "
                f"may still be live under a different label. Will retry "
                f"next sweep; resolve manually if persistent.[/red]"
            )
            return False
        # Ensure the manage page is fully loaded before we touch the table.
        try:
            await session.page.wait_for_load_state(
                "networkidle",
                timeout=15000,
            )
        except Exception:
            import logging

            logging.getLogger("scryland").debug(
                "networkidle timeout after click_manage — proceeding anyway"
            )
        await retry_on_flaky(
            lambda: pricing_page.set_quantity_zero(row["condition"]),
            page=session.page,
            label="delist: set_quantity_zero",
        )
        await retry_on_flaky(
            lambda: pricing_page.save_changes(),
            page=session.page,
            label="delist: save_changes",
        )
        console.print(
            f"  [red]TCG delist: '{row['product_name']}' ({row['condition']}) — sold on eBay[/red]"
        )
        # Update DB so we don't try again.
        db.conn.execute(
            "UPDATE inventory SET status = 'removed' WHERE id = ?",
            (row["id"],),
        )
        db.conn.commit()
        return True
    except Exception as exc:
        import logging

        logging.getLogger("scryland").warning(
            "Failed to delist TCG listing for canonical %s: %s: %s",
            canonical_key,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        console.print(f"  [red]TCG delist failed: {type(exc).__name__}: {exc}[/red]")
        return False


async def _tcg_floor_sweep(session, config, db, logger, threshold: float) -> int:
    """End TCG listings whose price is at or below `threshold`.

    Watch's main path uses TCG's Price Differential Report, which only
    flags rows where our price is *above* the market lowest. A card that
    has already drifted to the floor (e.g. $0.01) leaves no differential
    and would sit live indefinitely. This sweep targets only the rows we
    already know about in the DB at <= threshold, so it's cheap (1 manage
    nav per candidate, not per product).
    """
    from scryland.browser.flaky import retry_on_flaky
    from scryland.browser.pages.inventory import InventoryPage
    from scryland.browser.pages.pricing import PricingPage

    if session is None:
        return 0
    candidates = db.get_active_at_or_below_price(threshold)
    if not candidates:
        return 0

    console.print(
        f"  [dim]TCG floor sweep: {len(candidates)} listing(s) at or below ${threshold:.2f}…[/dim]"
    )
    inventory_page = InventoryPage(session.page, config)
    pricing_page = PricingPage(session.page, config)
    delisted = 0
    # Group by product_name so we save once per product even if multiple
    # condition rows are below the floor.
    by_product: dict[str, list[dict]] = {}
    for row in candidates:
        by_product.setdefault(row["product_name"], []).append(row)

    await retry_on_flaky(
        lambda: inventory_page.navigate(),
        page=session.page,
        label="floor: inventory.navigate",
    )
    for product_name, rows in by_product.items():
        try:
            await session.human_delay()
            await retry_on_flaky(
                lambda name=product_name: inventory_page.click_manage_for_product(name),
                page=session.page,
                label="floor: click_manage",
            )
            # Manage page navigation is async — wait for it to settle
            # before querying the pricing table, otherwise query_selector
            # races the navigation and raises "execution context destroyed".
            try:
                await session.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                logger.debug("floor: networkidle timeout after click_manage — proceeding")
            zeroed_rows: list[dict] = []
            for row in rows:
                try:
                    await retry_on_flaky(
                        lambda cond=row["condition"]: pricing_page.set_quantity_zero(cond),
                        page=session.page,
                        label="floor: set_quantity_zero",
                    )
                    zeroed_rows.append(row)
                except Exception:
                    logger.warning(
                        "Floor sweep: failed to zero qty for %s (%s)",
                        product_name,
                        row["condition"],
                        exc_info=True,
                    )
            if zeroed_rows:
                try:
                    await retry_on_flaky(
                        lambda: pricing_page.save_changes(),
                        page=session.page,
                        label="floor: save_changes",
                    )
                    # Only mark the rows we actually zeroed as removed —
                    # marking the whole `rows` group would falsely flag
                    # un-zeroed rows as removed in the DB while they're
                    # still live on TCG.
                    for row in zeroed_rows:
                        db.mark_inventory_removed(
                            row["product_name"],
                            row["condition"],
                            row["finish"] or "",
                        )
                    delisted += len(zeroed_rows)
                    console.print(
                        f"    [red]Delisted {len(zeroed_rows)}× '{product_name}' "
                        f"at or below ${threshold:.2f}[/red]"
                    )
                except Exception:
                    logger.warning(
                        "Floor sweep save failed for '%s' — %d delist(s) NOT persisted",
                        product_name,
                        len(zeroed_rows),
                        exc_info=True,
                    )
                    console.print(
                        f"    [red]Save failed — '{product_name}' delist(s) not applied[/red]"
                    )
        except Exception:
            logger.warning(
                "Floor sweep failed for '%s' — skipped this run",
                product_name,
                exc_info=True,
            )
        finally:
            # Always re-navigate to inventory before the next iteration.
            # Skipping this on a failure left the next click_manage_for_product
            # firing against a stale manage page, cascading the failure.
            # reapply_filter=True is critical: TCG drops 'My Inventory Only'
            # across the save→Back-to-Inventory redirect, so without
            # re-applying, the next iteration paginates the global catalog
            # instead of the user's listings.
            try:
                await session.human_delay()
                await retry_on_flaky(
                    lambda: inventory_page.go_back_to_inventory(reapply_filter=True),
                    page=session.page,
                    label="floor: go_back_to_inventory",
                )
            except Exception:
                logger.warning(
                    "Floor sweep: could not return to inventory after '%s' — "
                    "remaining products this run may be skipped",
                    product_name,
                    exc_info=True,
                )
    return delisted


async def _ebay_watch_pass(
    config,
    db,
    session,
    logger,
    *,
    min_price: float = 0.99,
    max_price: float | None = None,
    delist_below: float | None = None,
    delist_uncompetitive_gap: float | None = None,
) -> dict:
    """One iteration of the eBay watch sweep inside the main watch loop.

    Steps:
      1. Fetch recent eBay orders → record sales → mark our eBay listing sold.
      1b. Reconciliation sweep: re-check all sold eBay listings and ensure the
          matching TCG listing is also delisted (catches prior missed delistings).
      2. For each sold eBay listing, delist the matching TCG listing.
      3. For all TCG sales with cross_delist_done=0, withdraw matching eBay
         offers via the Sell API and mark them done.
      4. Price undercut sweep: for each active eBay listing, query Browse
         and update the offer price to undercut competitors by $0.01.
    Returns counts for the run summary.
    """
    from dataclasses import dataclass

    from scryland.ebay.auth import EbayAuth
    from scryland.ebay.client import EbayClient
    from scryland.ebay.orders import EbayOrdersClient, order_to_sales_rows

    console.print(
        f"\n[bold]eBay sweep[/bold] [dim](our shipping ${config.ebay_shipping_cost:.2f})[/dim]"
    )
    passphrase = _ebay_passphrase(config)
    auth = EbayAuth(config)
    try:
        await auth.access_token(passphrase)
    except Exception as exc:
        # Hint for the most common cause: refresh token expired/revoked.
        msg = str(exc)
        if "invalid_grant" in msg or "refresh" in msg.lower():
            console.print(
                "  [red]eBay auth failed — refresh token expired. "
                "Run [bold]scryland ebay-auth[/bold] to reconnect.[/red]"
            )
        else:
            console.print(f"  [red]eBay auth failed: {exc}[/red]")
        return _empty_ebay_result(error="auth")

    # ---- 1. eBay orders → record sales + mark listings sold
    orders_fetch_failed = False
    async with EbayOrdersClient(config, auth, passphrase) as orders_client:
        try:
            orders = await orders_client.iter_recent_orders()
        except Exception:
            logger.error("Fetching eBay orders failed", exc_info=True)
            console.print(
                "  [red]eBay orders fetch FAILED — sales may be missed "
                "this sweep; cross-marketplace delist skipped[/red]"
            )
            orders = []
            orders_fetch_failed = True

    new_ebay_sales = 0
    tcg_delist_failed = 0
    for order in orders:
        rows = order_to_sales_rows(order)
        n = db.record_order_sales(rows)
        new_ebay_sales += n
        for row in rows:
            sku = row.get("_sku")
            if not sku:
                continue
            ebay_listing = db.conn.execute(
                "SELECT canonical_key FROM ebay_listings WHERE sku = ?",
                (sku,),
            ).fetchone()
            if not ebay_listing:
                logger.warning(
                    "eBay sale SKU=%s has no ebay_listings row — TCG cross-delist skipped",
                    sku,
                )
                console.print(
                    f"    [yellow]No local record for sold SKU {sku}; "
                    f"TCG cross-delist skipped[/yellow]"
                )
                continue
            key = ebay_listing["canonical_key"]
            db.mark_ebay_listing_status(sku, "sold")
            # Cross-delist on TCG (uses the open browser session).
            if n:
                ok = await _end_tcg_listing_by_canonical(session, config, db, key)
                if not ok and session is not None:
                    tcg_delist_failed += 1
                    console.print(
                        f"    [red]TCG delist FAILED for sold eBay card "
                        f"(canonical: {key[:50]}…) — listing may still be "
                        f"live on TCG[/red]"
                    )
    if new_ebay_sales:
        console.print(f"  [green]{new_ebay_sales} new eBay sale(s) recorded.[/green]")
    else:
        console.print("  [dim]No new eBay sales.[/dim]")

    # ---- 1b. Reconciliation: sweep ALL sold eBay listings and make sure
    # the matching TCG listing is also delisted. Covers the case where a
    # sale was recorded in a previous run but the TCG delist didn't fire
    # or failed silently. In eBay-only mode (no browser), if there's
    # TCG-side cleanup to do we lazily spin up a browser just for this.
    #
    # If orders fetch failed earlier, we may be looking at stale ebay
    # listings data — still reconcile based on the DB, since new sold
    # rows can only be added, never removed without a deliberate
    # re-listing.
    sold_rows = db.conn.execute(
        "SELECT sku, canonical_key, product_name FROM ebay_listings WHERE status = 'sold'"
    ).fetchall()
    needs_delist = [
        r
        for r in sold_rows
        if r["canonical_key"] and db.find_inventory_by_canonical(r["canonical_key"])
    ]
    lazy_session_started = False
    if needs_delist and session is None:
        console.print(
            f"  [yellow]{len(needs_delist)} sold eBay card(s) still live on "
            f"TCG — spinning up browser to reconcile…[/yellow]"
        )
        from scryland.browser.session import BrowserSession

        candidate = BrowserSession(config)
        started = False
        try:
            await candidate.start()
            started = True
            await candidate.ensure_logged_in()
            session = candidate
            lazy_session_started = True
        except Exception as exc:
            logger.warning(
                "Failed to start lazy browser for TCG reconciliation: %s",
                exc,
                exc_info=True,
            )
            console.print(
                "  [red]Couldn't open browser — TCG reconciliation skipped; "
                "run a full (non --ebay-only) watch to catch up[/red]"
            )
            # If start() succeeded but login failed, we need to close the
            # half-opened Playwright context so we don't leak chromium.
            if started:
                try:
                    await candidate.close()
                except Exception:
                    logger.warning(
                        "Lazy session cleanup failed after login error — chromium process may leak",
                        exc_info=True,
                    )
            session = None

    if session is not None and needs_delist:
        reconciled = 0
        for row in needs_delist:
            key = row["canonical_key"]
            ok = await _end_tcg_listing_by_canonical(session, config, db, key)
            if ok:
                reconciled += 1
            else:
                tcg_delist_failed += 1
                console.print(
                    f"    [red]Reconciliation TCG delist FAILED for "
                    f"'{row['product_name']}' (sold on eBay earlier)[/red]"
                )
        if reconciled:
            console.print(
                f"  [yellow]Reconciled {reconciled} previously-sold eBay "
                f"card(s) — delisted matching TCG listing.[/yellow]"
            )

    # If we spun up the browser lazily, close it when reconciliation ends.
    if lazy_session_started and session is not None:
        try:
            await session.close()
        except Exception:
            logger.warning(
                "Failed to close lazy browser session — chromium process may leak",
                exc_info=True,
            )
        session = None

    # ---- 2. TCG sales → withdraw matching eBay offers
    async with EbayClient(config, auth, passphrase) as client:
        # TCG sales rows don't carry canonical keys; compute them from
        # (product_name, condition, finish-inference) and look for active
        # eBay listings that match.
        from scryland.db import canonical_key

        # Only consider sales we haven't already cross-delisted — the
        # sales.cross_delist_done flag is set True on a successful or
        # "no-match" pass so we don't re-process historical orders every
        # sweep (and don't accidentally withdraw a manually-republished
        # listing).
        recent_tcg = db.conn.execute(
            "SELECT id, product_name, condition FROM sales "
            "WHERE (marketplace = 'tcgplayer' OR marketplace IS NULL) "
            "AND (cross_delist_done IS NULL OR cross_delist_done = 0) "
            "ORDER BY id DESC LIMIT 200"
        ).fetchall()
        withdrawn = 0
        tcg_triggered_withdraw_failed = 0
        for row in recent_tcg:
            any_failure = False
            for foil in (False, True):
                key = canonical_key(row["product_name"], row["condition"], foil)
                match = db.find_ebay_listing_by_canonical(key)
                if not match or not match.get("offer_id"):
                    continue
                if await client.withdraw_offer(match["offer_id"]):
                    db.mark_ebay_listing_status(match["sku"], "ended")
                    console.print(
                        f"  [yellow]eBay delist: '{match['product_name']}' — sold on TCG[/yellow]"
                    )
                    withdrawn += 1
                else:
                    tcg_triggered_withdraw_failed += 1
                    any_failure = True
                    console.print(
                        f"  [red]eBay delist FAILED for '{match['product_name']}' "
                        f"(sold on TCG) — listing may still be live, will "
                        f"retry next sweep[/red]"
                    )
            # Only mark the sale as done when there's nothing left to do:
            # either no eBay side existed, or every matched eBay listing
            # was successfully withdrawn. A failed withdraw leaves the
            # flag at 0 so the next sweep retries.
            if not any_failure:
                db.conn.execute(
                    "UPDATE sales SET cross_delist_done = 1 WHERE id = ?",
                    (row["id"],),
                )
        if recent_tcg:
            db.conn.commit()
        if withdrawn:
            console.print(f"  Withdrew {withdrawn} eBay offer(s) after TCG sale.")

        # ---- 3. Undercut sweep: for each active eBay listing, find current
        # Browse lowest and update price. Phase 1 fetches all competitor
        # prices in parallel (network-bound, safe to fan out). Phase 2
        # applies updates serially so log output stays coherent.
        import asyncio

        active_ebay = db.get_ebay_listings(status="active")

        # Seed the seller-username lookup with one of our listing_ids
        # before the parallel fan-out, so the 8 concurrent tasks don't
        # all race to hit /identity. Uses Browse API on one of our live
        # listings — pulls the seller.username from the response.
        sample_listing_id = next(
            (row["listing_id"] for row in active_ebay if row.get("listing_id")),
            None,
        )
        if sample_listing_id:
            await client.get_own_seller_username(sample_listing_id)

        if active_ebay:
            console.print(
                f"  Checking {len(active_ebay)} eBay listing(s) for undercut "
                f"(floor ${min_price:.2f}"
                + (f", ceiling ${max_price:.2f}" if max_price else "")
                + (f", delist<${delist_below:.2f}" if delist_below else "")
                + (
                    f", delist if >TCG+${delist_uncompetitive_gap:.2f}"
                    if delist_uncompetitive_gap
                    else ""
                )
                + ")…"
            )
            updated = 0
            delisted = 0
            browse_errors = 0
            skipped_big_drops = 0
            price_changes: list[dict] = []
            delisted_items: list[dict] = []
            first_browse_err: Exception | None = None

            # Phase 1: parallel Browse lookups with a concurrency cap to
            # avoid tripping eBay rate limits.
            sem = asyncio.Semaphore(8)

            async def _lookup(lst: dict) -> tuple[dict, float | None, Exception | None]:
                if not lst.get("offer_id"):
                    return lst, None, None
                current = lst["price"] or 0
                if max_price is not None and current > max_price:
                    return lst, None, None
                async with sem:
                    try:
                        lowest = await client.find_lowest_price(
                            lst["product_name"],
                            lst["set_name"],
                            lst["collector_number"],
                            bool(lst["is_foil"]),
                            condition=lst["condition"],
                            include_foil=bool(lst["is_foil"]),
                        )
                    except Exception as exc:
                        return lst, None, exc
                return lst, lowest, None

            lookup_results = await asyncio.gather(*(_lookup(lst) for lst in active_ebay))

            # Phase 2: compute decisions (pure), then fan out write calls.
            # Decisions collected up front so logs stay clean even with
            # parallel writes. DB upserts + console prints happen serially
            # after the API calls come back so output stays ordered.
            our_ship = config.ebay_shipping_cost

            @dataclass
            class _PlannedUpdate:
                lst: dict
                current: float
                target: float

            @dataclass
            class _PlannedWithdraw:
                lst: dict
                market_low: float
                reason: str = "market_below_floor"  # or "uncompetitive_vs_tcg"
                tcg_price: float | None = None
                our_price: float | None = None

            planned_updates: list[_PlannedUpdate] = []
            planned_withdraws: list[_PlannedWithdraw] = []
            # Counters for the uncompetitive-vs-TCG path — surface why
            # listings aren't being delisted when the user expects them
            # to be (most often: TCG inventory table is missing rows).
            uncomp_no_canonical = 0
            uncomp_no_tcg_row = 0
            uncomp_no_tcg_price = 0
            uncomp_gap_too_small = 0

            for lst, lowest, exc in lookup_results:
                if exc is not None:
                    browse_errors += 1
                    if first_browse_err is None:
                        first_browse_err = exc
                    continue
                if lowest is None:
                    continue
                current = lst["price"] or 0

                # Delist if the market has collapsed below our floor.
                if delist_below is not None and lowest < delist_below:
                    planned_withdraws.append(_PlannedWithdraw(lst=lst, market_low=float(lowest)))
                    continue

                # Delist if our eBay total is meaningfully above the TCG
                # total for the same canonical card. Buyers comparison-shop
                # TCG; if we're $0.50+ over with the same shipping, the
                # listing is dead weight. Assumes TCG shipping ≈ our eBay
                # shipping (config.ebay_shipping_cost), so the price-gap
                # condition collapses to: our_ebay_price - tcg_price > gap.
                if delist_uncompetitive_gap is not None:
                    canonical = lst.get("canonical_key")
                    tcg_row = db.find_inventory_by_canonical(canonical) if canonical else None
                    tcg_price = tcg_row["current_price"] if tcg_row else None
                    name_short = (lst.get("product_name") or "?")[:40]
                    if not canonical:
                        uncomp_no_canonical += 1
                        logger.debug(
                            "uncompetitive: '%s' has no canonical_key — can't compare to TCG",
                            name_short,
                        )
                    elif tcg_row is None:
                        uncomp_no_tcg_row += 1
                        logger.debug(
                            "uncompetitive: '%s' canonical=%s not in TCG inventory — "
                            "can't compare. Run add-inventory or scrape TCG to populate.",
                            name_short,
                            canonical[:60],
                        )
                    elif tcg_price is None:
                        uncomp_no_tcg_price += 1
                        logger.debug(
                            "uncompetitive: '%s' found TCG row but current_price is null",
                            name_short,
                        )
                    elif current <= 0:
                        logger.debug(
                            "uncompetitive: '%s' our eBay price is %s — can't compare",
                            name_short,
                            current,
                        )
                    else:
                        gap = float(current) - float(tcg_price)
                        if gap > delist_uncompetitive_gap:
                            logger.debug(
                                "uncompetitive: '%s' our=$%.2f TCG=$%.2f gap=$%.2f "
                                "> threshold $%.2f — withdrawing",
                                name_short,
                                float(current),
                                float(tcg_price),
                                gap,
                                delist_uncompetitive_gap,
                            )
                            planned_withdraws.append(
                                _PlannedWithdraw(
                                    lst=lst,
                                    market_low=float(lowest),
                                    reason="uncompetitive_vs_tcg",
                                    tcg_price=float(tcg_price),
                                    our_price=float(current),
                                )
                            )
                            continue
                        uncomp_gap_too_small += 1
                        logger.debug(
                            "uncompetitive: '%s' our=$%.2f TCG=$%.2f gap=$%.2f "
                            "<= threshold $%.2f — keeping",
                            name_short,
                            float(current),
                            float(tcg_price),
                            gap,
                            delist_uncompetitive_gap,
                        )

                # Total-price undercut math — always respect BOTH the
                # eBay hard $0.99 minimum AND the user's --ebay-min-price.
                target_total = lowest - 0.01
                undercut_target = round(target_total - our_ship, 2)
                target = round(max(undercut_target, min_price, 0.99), 2)
                if abs(target - current) < 0.01:
                    continue
                # Guardrail: skip big drops unless volatile. Combines pct
                # AND absolute thresholds so penny moves (e.g. $0.04 → $0.03)
                # aren't held up by their large percentage.
                from scryland.pricing.guardrails import is_big_price_drop as _is_big_drop

                if _is_big_drop(
                    float(current),
                    float(target),
                    config.max_price_change_pct,
                    config.max_price_change_abs,
                ):
                    skipped_big_drops += 1
                    console.print(
                        f"    [yellow]{lst['product_name'][:40]} "
                        f"skip big drop ${current:.2f} → ${target:.2f} "
                        f"(>{config.max_price_change_pct:.0f}% AND "
                        f">${config.max_price_change_abs:.2f})[/yellow]"
                    )
                    continue
                planned_updates.append(
                    _PlannedUpdate(lst=lst, current=float(current), target=float(target))
                )

            # Parallel withdraws first. Lock DB writes behind a lock since
            # sqlite3 connections aren't thread-safe (but asyncio is single-
            # threaded so no locks needed for DB here — writes interleave
            # with API calls within the single event loop).
            async def _do_withdraw(p: _PlannedWithdraw) -> bool:
                try:
                    async with sem:
                        return await client.withdraw_offer(p.lst["offer_id"])
                except Exception as exc:
                    logger.warning(
                        "withdraw offer raised for %s: %s",
                        p.lst["product_name"],
                        exc,
                        exc_info=True,
                    )
                    return False

            async def _do_update(p: _PlannedUpdate) -> bool:
                try:
                    async with sem:
                        return await client.update_offer_price(
                            p.lst["offer_id"],
                            p.target,
                            p.lst["quantity"],
                        )
                except Exception as exc:
                    logger.warning(
                        "update offer raised for %s: %s",
                        p.lst["product_name"],
                        exc,
                        exc_info=True,
                    )
                    return False

            # return_exceptions=True so one failure can't cancel siblings
            # mid-PUT — combined with the per-task try/except above, each
            # task now always produces a bool.
            withdraw_ok, update_ok = await asyncio.gather(
                asyncio.gather(
                    *(_do_withdraw(p) for p in planned_withdraws),
                    return_exceptions=True,
                ),
                asyncio.gather(
                    *(_do_update(p) for p in planned_updates),
                    return_exceptions=True,
                ),
            )

            # Serialize DB writes + ordered console output now that all
            # API calls have returned.
            withdraw_failed = 0
            for p, ok in zip(planned_withdraws, withdraw_ok, strict=True):
                # `return_exceptions=True` can yield BaseException (e.g.
                # CancelledError from Ctrl-C or timeout) that our inner
                # Exception-handler didn't catch. Treat those as failures
                # so the DB doesn't get marked "ended" for a withdraw
                # that never completed.
                if isinstance(ok, BaseException):
                    logger.error(
                        "withdraw task for %s raised %r",
                        p.lst["product_name"],
                        ok,
                        exc_info=ok,
                    )
                    withdraw_failed += 1
                    console.print(
                        f"    [red]{p.lst['product_name'][:40]} "
                        f"withdraw task crashed — listing may still be live[/red]"
                    )
                    continue
                if not ok:
                    withdraw_failed += 1
                    console.print(
                        f"    [red]{p.lst['product_name'][:40]} "
                        f"withdraw FAILED — listing may still be live[/red]"
                    )
                    continue
                db.mark_ebay_listing_status(p.lst["sku"], "ended")
                delisted += 1
                delisted_items.append(
                    {
                        "name": p.lst["product_name"],
                        "market_low": p.market_low,
                        "reason": p.reason,
                        "tcg_price": p.tcg_price,
                    }
                )
                if p.reason == "uncompetitive_vs_tcg":
                    console.print(
                        f"    [red]{p.lst['product_name'][:40]} "
                        f"our ${(p.our_price or 0):.2f} vs TCG "
                        f"${p.tcg_price:.2f} (gap >${delist_uncompetitive_gap:.2f}) "
                        f"— withdrew listing[/red]"
                    )
                else:
                    console.print(
                        f"    [red]{p.lst['product_name'][:40]} "
                        f"eBay lowest ${p.market_low:.2f} < ${delist_below:.2f} "
                        f"— withdrew listing[/red]"
                    )

            update_failed = 0
            for p, ok in zip(planned_updates, update_ok, strict=True):
                if isinstance(ok, BaseException):
                    logger.error(
                        "update task for %s raised %r",
                        p.lst["product_name"],
                        ok,
                        exc_info=ok,
                    )
                    update_failed += 1
                    console.print(
                        f"    [red]{p.lst['product_name'][:40]} "
                        f"update task crashed — price NOT changed[/red]"
                    )
                    continue
                if not ok:
                    update_failed += 1
                    console.print(
                        f"    [red]{p.lst['product_name'][:40]} "
                        f"update FAILED — price NOT changed[/red]"
                    )
                    continue
                lst = p.lst
                db.upsert_ebay_listing(
                    sku=lst["sku"],
                    offer_id=lst["offer_id"],
                    listing_id=lst["listing_id"],
                    product_name=lst["product_name"],
                    set_name=lst["set_name"],
                    collector_number=lst["collector_number"],
                    condition=lst["condition"],
                    is_foil=bool(lst["is_foil"]),
                    price=p.target,
                    quantity=lst["quantity"],
                    status="active",
                )
                updated += 1
                price_changes.append(
                    {
                        "name": lst["product_name"],
                        "old": p.current,
                        "new": p.target,
                    }
                )
                # Record snapshot for price-history.
                try:
                    db.record_ebay_price(
                        product_name=lst["product_name"],
                        condition=lst["condition"],
                        our_price=p.target,
                    )
                except Exception:
                    logger.debug("record_ebay_price failed", exc_info=True)
                console.print(
                    f"    [green]{lst['product_name'][:40]} "
                    f"${p.current:.2f} → ${p.target:.2f}[/green]"
                )
            summary = f"  Updated {updated}/{len(active_ebay)} eBay prices."
            if delisted:
                summary += f" Delisted {delisted}."
            if update_failed:
                summary += f" {update_failed} update failed."
            if withdraw_failed:
                summary += f" {withdraw_failed} withdraw failed."
            if browse_errors:
                summary += f" {browse_errors} Browse error(s)."
                if first_browse_err is not None:
                    logger.warning(
                        "Browse query errors in undercut sweep (first: %s)",
                        first_browse_err,
                        exc_info=first_browse_err,
                    )
            console.print(summary)
            # Visibility for the uncompetitive-delist path. If the user
            # passes --ebay-delist-uncompetitive-gap but sees no delists,
            # this line tells them why at a glance — usually "TCG row
            # missing" because the inventory table isn't synced.
            if delist_uncompetitive_gap is not None:
                parts = []
                if uncomp_no_tcg_row:
                    parts.append(f"{uncomp_no_tcg_row} no TCG row")
                if uncomp_no_canonical:
                    parts.append(f"{uncomp_no_canonical} no canonical_key")
                if uncomp_no_tcg_price:
                    parts.append(f"{uncomp_no_tcg_price} TCG price null")
                if uncomp_gap_too_small:
                    parts.append(f"{uncomp_gap_too_small} gap under threshold")
                if parts:
                    console.print(f"  [dim]Uncompetitive check skipped: {', '.join(parts)}.[/dim]")
            return {
                "new_sales": new_ebay_sales,
                "updated": updated,
                "delisted": delisted,
                "withdrawn": withdrawn,
                "checked": len(active_ebay),
                "changes": price_changes,
                "delisted_items": delisted_items,
                "tcg_delist_failed": tcg_delist_failed,
                "browse_errors": browse_errors,
                "skipped_big_drops": skipped_big_drops,
                "withdraw_failed": withdraw_failed,
                "update_failed": update_failed,
                "tcg_triggered_withdraw_failed": tcg_triggered_withdraw_failed,
                "orders_fetch_failed": orders_fetch_failed,
                "error": None,
            }
        return {
            "new_sales": new_ebay_sales,
            "updated": 0,
            "delisted": 0,
            "withdrawn": withdrawn,
            "checked": 0,
            "changes": [],
            "delisted_items": [],
            "tcg_delist_failed": tcg_delist_failed,
            "browse_errors": 0,
            "skipped_big_drops": 0,
            "withdraw_failed": 0,
            "update_failed": 0,
            "tcg_triggered_withdraw_failed": tcg_triggered_withdraw_failed,
            "orders_fetch_failed": orders_fetch_failed,
            "error": None,
        }


def _build_sales_rows(details: dict, fallback_order_num: str = "") -> list[dict]:
    """Build a list of per-product sale dicts from an order details blob.

    Keeps the field mapping in one place so `optimize`, `watch`, and `sales`
    stay consistent.
    """
    from scryland.browser.pages.orders import parse_product_name

    order_num = details.get("order_number") or fallback_order_num
    rows: list[dict] = []
    for product in details.get("products", []):
        card_name, condition = parse_product_name(product["raw_product"])
        rows.append(
            {
                "order_number": order_num,
                "order_date": details.get("order_date", ""),
                "buyer_name": details.get("buyer_name", ""),
                "status": details.get("status", ""),
                "product_name": card_name,
                "condition": condition,
                "quantity": product.get("quantity", 1),
                "sale_price": product.get("sale_price", 0),
                "shipping_amt": details.get("shipping_amt", 0),
                "total_amount": details.get("total_amount", 0),
                "fee_amount": details.get("fee_amount", 0),
                "net_amount": details.get("net_amount", 0),
            }
        )
    return rows


@click.group()
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="Logging level",
)
@click.option(
    "--no-delays",
    is_flag=True,
    default=False,
    help="Skip all artificial wait/sleep calls (faster, less human-like)",
)
@click.pass_context
def cli(ctx: click.Context, log_level: str | None, no_delays: bool) -> None:
    """Scryland — multi-marketplace MTG seller toolkit.

    Manages TCGPlayer inventory (via browser automation) and eBay listings
    (via the Sell API). Keeps prices competitive, tracks sales across both
    marketplaces, and auto-removes sold items from the other side.

    \b
    Common flows:
      scryland add-inventory cards.csv       # list on TCGPlayer from CSV
      scryland list-on-ebay cards.csv        # list on eBay from CSV
      scryland compare                       # TCG vs eBay side-by-side
      scryland watch                         # recurring price optimize
      scryland watch --ebay-only             # eBay-only, no browser

    First-time eBay setup, in order:
      scryland ebay-auth                     # OAuth consent
      scryland ebay-bootstrap --city ... \\
        --state ... --postal-code ...        # policies + location
    """
    ctx.ensure_object(dict)
    overrides: dict = {}
    if log_level:
        overrides["log_level"] = log_level.upper()
    if no_delays:
        overrides["no_delays"] = True
    config = ScrylandConfig(**overrides)
    ctx.obj["config"] = config
    ctx.obj["logger"] = setup_logging(config)


@cli.command()
@click.option("--dry-run", is_flag=True, default=False, help="Calculate changes but don't apply")
@click.option(
    "--volatile",
    is_flag=True,
    default=False,
    help="Skip the 10%% change confirmation — auto-approve all price changes",
)
@click.option(
    "--delist-below",
    type=float,
    default=None,
    help="Remove listings where TCG Lowest drops below this price (e.g., 0.40)",
)
@click.pass_context
def optimize(ctx: click.Context, dry_run: bool, volatile: bool, delist_below: float | None) -> None:
    """Run price optimization across TCGPlayer listings.

    Uses the Price Differential Report (much faster than scraping each
    product individually). For each row:
      - Confirms large price drops (> max_price_change_pct).
      - Skips increases and penny-delist cases automatically.
      - Delists (qty=0 + save) when lowest is ≤ $0.01.

    After the optimize pass, syncs inventory to the DB and optionally
    delists items where TCG Lowest dropped below --delist-below.
    """
    config: ScrylandConfig = ctx.obj["config"]
    if dry_run:
        config = config.model_copy(update={"dry_run": True})
    if volatile:
        config = config.model_copy(update={"max_price_change_pct": 999.0})
        console.print("[yellow]Volatile mode — all price changes will be auto-approved.[/yellow]")
    logger = ctx.obj["logger"]

    if config.dry_run:
        console.print("[yellow]DRY RUN — no changes will be applied[/yellow]")

    async def _run() -> None:
        from scryland.browser.pages.inventory import InventoryPage
        from scryland.browser.pages.pricing import PricingPage
        from scryland.browser.session import BrowserSession
        from scryland.db import InventoryDB
        from scryland.pricing.optimizer import run_price_differential_optimize

        session = BrowserSession(config)
        db = InventoryDB(Path(config.db_path))

        try:
            db.open()
            await session.start()
            await session.ensure_logged_in()

            # Use the Price Differential Report — much faster than checking each product
            console.print("Checking price differential report...")
            opt_result = await run_price_differential_optimize(session, config, console)
            if opt_result.total:
                console.print(
                    f"\n[bold]Updated {opt_result.updated} listing(s), "
                    f"delisted {opt_result.delisted}, skipped {opt_result.skipped}, "
                    f"failed {opt_result.failed}.[/bold]"
                )
            # Pricing page used for --delist-below sweep below
            pricing_page = PricingPage(session.page, config)

            # Sync inventory to DB + check for delist candidates
            console.print("\nSyncing inventory to database...")
            inventory_page = InventoryPage(session.page, config)
            await inventory_page.navigate()
            products = await inventory_page.get_product_names()
            all_listings = []
            delisted = 0
            scrape_failures = 0
            for product in products:
                try:
                    await session.human_delay()
                    await inventory_page.click_manage_for_product(product["name"])
                    listings = await inventory_page.get_manage_page_listings(product["name"])
                    all_listings.extend(listings)

                    # Check for delist candidates
                    if delist_below and not config.dry_run:
                        pending_delist = 0
                        for listing in listings:
                            tcg_low = (
                                float(listing.tcg_low_price) if listing.tcg_low_price else None
                            )
                            if tcg_low is not None and tcg_low < delist_below:
                                console.print(
                                    f"  [red]Delisting {listing.product_name} "
                                    f"({listing.condition}) — TCG Low ${tcg_low:.2f} "
                                    f"< ${delist_below:.2f}[/red]"
                                )
                                try:
                                    await pricing_page.set_quantity_zero(listing.condition)
                                    pending_delist += 1
                                except Exception:
                                    logger.warning(
                                        "Failed to zero qty for %s (%s)",
                                        listing.product_name,
                                        listing.condition,
                                        exc_info=True,
                                    )
                        if pending_delist:
                            await session.human_delay()
                            try:
                                await pricing_page.save_changes()
                                delisted += pending_delist
                            except Exception:
                                logger.warning(
                                    "save_changes failed — %d delist(s) for '%s' "
                                    "were NOT persisted",
                                    pending_delist,
                                    product["name"],
                                    exc_info=True,
                                )
                                console.print("    [red]Save failed — delist(s) not applied[/red]")

                    await session.human_delay()
                    await inventory_page.go_back_to_inventory(reapply_filter=True)
                except Exception:
                    logger.warning(
                        "Could not scrape '%s' for DB sync — product skipped "
                        "(won't affect its DB row this run)",
                        product["name"],
                        exc_info=True,
                    )
                    scrape_failures += 1

            if scrape_failures:
                console.print(
                    f"  [yellow]Warning: {scrape_failures} product(s) failed to "
                    f"scrape — DB sync may mark them as removed on the next run[/yellow]"
                )
            if all_listings:
                sync_report = db.sync(all_listings)
                console.print(
                    f"  Database synced: [cyan]{sync_report.total_active}[/cyan] active listings"
                )
            if delisted:
                console.print(
                    f"  [red]Delisted {delisted} listing(s) below ${delist_below:.2f}[/red]"
                )

            # Check for new sales while we're logged in
            try:
                from scryland.browser.pages.orders import OrdersPage

                console.print("\nChecking for new sales...")
                orders_page = OrdersPage(session.page, config)
                await orders_page.navigate()
                await session.dismiss_popups()

                known_orders = db.get_known_order_numbers()
                order_rows = await orders_page.get_order_rows()

                new_orders = [o for o in order_rows if o.get("order_number") not in known_orders]
                if not new_orders:
                    console.print("  No new sales.")
                else:
                    new_sales = 0
                    for order in new_orders:
                        order_num = order.get("order_number", "")
                        href = order.get("order_href", "")
                        if not href:
                            continue
                        await session.human_delay()
                        try:
                            details = await orders_page.get_order_details(href)
                            sales_for_order = _build_sales_rows(details, order_num)
                            new_sales += db.record_order_sales(sales_for_order)
                            await orders_page.go_back_to_orders()
                        except Exception:
                            logger.warning(
                                "Failed to scrape order %s — order left unrecorded",
                                order_num,
                                exc_info=True,
                            )

                    if new_sales:
                        summary = db.get_sales_summary()
                        console.print(f"  [green]{new_sales} new sale(s) recorded![/green]")
                        console.print(
                            f"  Net income to date: [green]${summary['total_net']:.2f}[/green]"
                        )
                    else:
                        console.print("  No new sales.")
            except Exception:
                logger.warning("Sales check failed", exc_info=True)
                console.print("  [yellow]Sales check skipped — see log for details[/yellow]")

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception:
            logger.exception("Optimization failed")
            sys.exit(1)
        finally:
            db.close()
            try:
                await session.close()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")


@cli.command()
@click.option(
    "--snapshot", is_flag=True, default=False, help="Save screenshot + HTML of inventory page"
)
@click.pass_context
def explore(ctx: click.Context, snapshot: bool) -> None:
    """Open browser and pause for interactive exploration.

    Useful for discovering selectors and page structure.
    Use --snapshot to save a screenshot and HTML dump of the inventory page.
    """
    config: ScrylandConfig = ctx.obj["config"]
    config = config.model_copy(update={"headless": False})

    async def _run() -> None:
        from pathlib import Path

        from scryland.browser.session import BrowserSession

        session = BrowserSession(config)
        try:
            await session.start()
            await session.ensure_logged_in()

            if snapshot:
                # Navigate to inventory and capture the page
                console.print("Navigating to inventory page...")
                await session.page.goto(
                    config.seller_portal_url,
                    wait_until="networkidle",
                )
                # Wait a bit for dynamic content to render
                await session.page.wait_for_timeout(3000)

                snapshot_dir = Path("snapshots")
                snapshot_dir.mkdir(exist_ok=True)

                # Screenshot
                screenshot_path = snapshot_dir / "inventory.png"
                await session.page.screenshot(path=str(screenshot_path), full_page=True)
                console.print(f"[green]Screenshot saved: {screenshot_path}[/green]")

                # HTML dump
                html_path = snapshot_dir / "inventory.html"
                html = await session.page.content()
                html_path.write_text(html)
                console.print(f"[green]HTML saved: {html_path}[/green]")

                console.print("\nBrowser is still open for manual inspection.")
                console.print("Use DevTools (F12) to explore elements.")
                console.print("Close the browser window when done.")
            else:
                console.print("[green]Logged in. Browser is open for exploration.[/green]")
                console.print("Use browser DevTools (F12) to inspect elements.")
                console.print("Close the browser window when done.")

            await session.page.wait_for_event("close", timeout=0)
        except KeyboardInterrupt:
            pass
        except Exception:
            logger.debug("Explore session ended", exc_info=True)
        finally:
            try:
                await session.close()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[green]Goodbye![/green]")


@cli.command()
@click.pass_context
def login(ctx: click.Context) -> None:
    """Log in to TCGPlayer and save the browser session for reuse.

    Opens a Chromium window; log in manually. The persistent cookie jar
    means most subsequent commands auto-login without prompting.
    """
    config: ScrylandConfig = ctx.obj["config"]
    # Force non-headless so the user can see the browser
    config = config.model_copy(update={"headless": False})

    async def _run() -> None:
        from scryland.browser.session import BrowserSession

        session = BrowserSession(config)
        try:
            await session.start()
            await session.ensure_logged_in()
            console.print("[green]Login successful! Session saved.[/green]")
            console.print("You can close the browser window or press Ctrl+C.")
            await session.page.wait_for_event("close", timeout=0)
        except KeyboardInterrupt:
            pass
        except Exception:
            logger.debug("Login session ended", exc_info=True)
        finally:
            try:
                await session.close()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[green]Session saved. Goodbye![/green]")


@cli.group()
def credentials() -> None:
    """Manage stored login credentials."""


@credentials.command("set")
def credentials_set() -> None:
    """Store encrypted login credentials for auto-login."""
    from rich.prompt import Prompt

    from scryland.credentials import save_credentials

    console.print("[bold]Store TCGPlayer login credentials[/bold]")
    console.print("Credentials will be encrypted with a passphrase you choose.")
    console.print("You'll need this passphrase each time auto-login runs.\n")

    username = Prompt.ask("TCGPlayer email/username")
    if not username.strip():
        console.print("[red]Username cannot be empty.[/red]")
        return
    password = Prompt.ask("TCGPlayer password", password=True)
    if not password:
        console.print("[red]Password cannot be empty.[/red]")
        return
    passphrase = Prompt.ask("Choose an encryption passphrase", password=True)
    if not passphrase:
        console.print("[red]Passphrase cannot be empty.[/red]")
        return
    passphrase_confirm = Prompt.ask("Confirm passphrase", password=True)

    if passphrase != passphrase_confirm:
        console.print("[red]Passphrases don't match. Try again.[/red]")
        return

    save_credentials(username, password, passphrase)
    console.print("[green]Credentials saved and encrypted![/green]")


@credentials.command("clear")
def credentials_clear() -> None:
    """Delete stored credentials."""
    from rich.prompt import Confirm

    from scryland.credentials import clear_credentials, credentials_exist

    if not credentials_exist():
        console.print("No stored credentials found.")
        return

    if Confirm.ask("Delete stored credentials?", default=False):
        clear_credentials()
        console.print("[green]Credentials deleted.[/green]")


@credentials.command("status")
def credentials_status() -> None:
    """Check if credentials are stored."""
    from scryland.credentials import credentials_exist

    if credentials_exist():
        console.print("[green]Encrypted credentials are stored.[/green]")
    else:
        console.print(
            "No stored credentials. Run [cyan]scryland credentials set[/cyan] to store them."
        )


@cli.command("reset-db")
@click.pass_context
def reset_db(ctx: click.Context) -> None:
    """Delete the local inventory database. Does NOT touch TCGPlayer/eBay.

    Useful if the DB gets into a weird state. Run `scryland sync` afterwards
    to repopulate from your live TCGPlayer inventory.
    """
    from rich.prompt import Confirm

    config: ScrylandConfig = ctx.obj["config"]
    db_path = Path(config.db_path)

    if not db_path.exists():
        console.print("No database found — nothing to reset.")
        return

    if not Confirm.ask(
        f"Delete inventory database at [cyan]{db_path}[/cyan]? This cannot be undone",
        default=False,
    ):
        console.print("Cancelled.")
        return

    db_path.unlink()
    console.print("[green]Database cleared. Run [cyan]scryland sync[/cyan] to repopulate.[/green]")


@cli.command("clear-sold")
@click.pass_context
def clear_sold(ctx: click.Context) -> None:
    """Delete rows with status = 'sold' or 'removed' from the local DB.

    Only affects the local cache. Real TCGPlayer/eBay data is untouched.
    """
    from scryland.db import InventoryDB

    config: ScrylandConfig = ctx.obj["config"]
    db_path = Path(config.db_path)

    if not db_path.exists():
        console.print("No database found.")
        return

    db = InventoryDB(db_path)
    db.open()
    try:
        sold = db.get_all_sold()
        if not sold:
            console.print("No sold/removed items in the database.")
            return

        console.print(f"Found [yellow]{len(sold)}[/yellow] sold/removed items:\n")
        for item in sold:
            price = f"${item['current_price']:.2f}" if item["current_price"] else "-"
            console.print(
                f"  - {item['product_name']} ({item['condition'] or 'Unknown'}) — {price}"
            )

        console.print()
        from rich.prompt import Confirm

        if Confirm.ask("Remove these from the database?", default=True):
            count = db.clear_sold()
            console.print(f"[green]Removed {count} sold items from database.[/green]")
        else:
            console.print("Cancelled.")
    finally:
        db.close()


@cli.command()
@click.option(
    "--refresh", is_flag=True, default=False, help="Sync from TCGPlayer before showing status"
)
@click.pass_context
def status(ctx: click.Context, refresh: bool) -> None:
    """Show current inventory from local database.

    Use --refresh to sync from TCGPlayer first.
    """
    config: ScrylandConfig = ctx.obj["config"]
    logger = ctx.obj["logger"]

    from scryland.db import InventoryDB

    db = InventoryDB(Path(config.db_path))
    db.open()

    # Optional live refresh
    if refresh:

        async def _sync() -> None:
            from scryland.browser.pages.inventory import InventoryPage
            from scryland.browser.session import BrowserSession

            session = BrowserSession(config)
            try:
                await session.start()
                await session.ensure_logged_in()

                inventory_page = InventoryPage(session.page, config)
                await inventory_page.navigate()
                products = await inventory_page.get_product_names()
                console.print(f"Syncing {len(products)} products...")

                all_listings = []
                for product in products:
                    try:
                        await session.human_delay()
                        await inventory_page.click_manage_for_product(product["name"])
                        listings = await inventory_page.get_manage_page_listings(product["name"])
                        all_listings.extend(listings)
                        await session.human_delay()
                        await inventory_page.go_back_to_inventory(reapply_filter=True)
                    except Exception:
                        logger.debug("Could not scrape '%s'", product["name"])

                sync_report = db.sync(all_listings)
                console.print(
                    f"[green]Synced! {sync_report.total_active} active listings.[/green]\n"
                )
            finally:
                try:
                    await session.close()
                except Exception:
                    pass

        try:
            asyncio.run(_sync())
        except KeyboardInterrupt:
            console.print("[yellow]Sync interrupted.[/yellow]")

    try:
        summary = db.get_summary()
        active = db.get_all_active()

        if not active:
            console.print(
                "[yellow]No inventory data. Run [cyan]scryland sync[/cyan] first.[/yellow]"
            )
            return

        # Format last sync time
        last_sync = summary["last_sync"]
        if last_sync != "Never":
            try:
                from datetime import datetime as dt

                ts = dt.fromisoformat(last_sync)
                last_sync = ts.strftime("%Y-%m-%d %I:%M %p")
            except Exception:
                last_sync = last_sync[:19]

        console.print()
        summary_table = Table(
            title="Inventory Summary", show_header=False, box=None, padding=(0, 2)
        )
        summary_table.add_column("Label", style="bold")
        summary_table.add_column("Value")
        summary_table.add_row("Active listings", f"[cyan]{summary['active_listings']}[/cyan]")
        summary_table.add_row("Total quantity", f"[cyan]{summary['total_quantity']}[/cyan]")
        summary_table.add_row("Total value", f"[green]${summary['total_value']:.2f}[/green]")
        summary_table.add_row("Sold/removed", f"[yellow]{summary['sold_count']}[/yellow]")
        summary_table.add_row("Last sync", last_sync)
        console.print(summary_table)

        t = Table(title="\nActive Inventory")
        t.add_column("Product", style="cyan")
        t.add_column("Condition")
        t.add_column("Price", justify="right", style="green")
        t.add_column("TCG Low", justify="right")
        t.add_column("Qty", justify="right")
        t.add_column("Last Seen")

        total_price = 0.0
        total_tcg_low = 0.0
        total_qty = 0
        for item in active:
            p = item["current_price"] or 0
            tl = item["tcg_low_price"] or 0
            q = item["quantity"] or 0
            total_price += p * q
            total_tcg_low += tl * q
            total_qty += q

            price = f"${p:.2f}" if item["current_price"] else "-"
            tcg_low = f"${tl:.2f}" if item["tcg_low_price"] else "-"
            last_seen = item["last_seen"][:10] if item["last_seen"] else "-"
            t.add_row(
                item["product_name"],
                item["condition"] or "",
                price,
                tcg_low,
                str(q),
                last_seen,
            )

        # Totals row
        t.add_section()
        t.add_row(
            "[bold]Totals[/bold]",
            "",
            f"[bold]${total_price:.2f}[/bold]",
            f"[bold]${total_tcg_low:.2f}[/bold]",
            f"[bold]{total_qty}[/bold]",
            "",
        )
        console.print(t)

        sold = db.get_all_sold()
        if sold:
            st = Table(title="\nSold/Removed Items")
            st.add_column("Product", style="cyan")
            st.add_column("Condition")
            st.add_column("Last Price", justify="right")
            st.add_column("Last Seen", max_width=12)
            for item in sold:
                price = f"${item['current_price']:.2f}" if item["current_price"] else "-"
                last_seen = item["last_seen"][:10] if item["last_seen"] else "-"
                st.add_row(item["product_name"], item["condition"] or "", price, last_seen)
            console.print(st)
            console.print(
                "\n  Run [cyan]scryland clear-sold[/cyan] to remove sold items from the database."
            )

        console.print(
            "\n  [dim]Commands: [cyan]scryland sync[/cyan] (update from TCGPlayer) | "
            "[cyan]scryland optimize[/cyan] (match lowest prices) | "
            "[cyan]scryland clear-sold[/cyan] (clean up) | "
            "[cyan]scryland reset-db[/cyan] (start fresh)[/dim]"
        )
    finally:
        db.close()


async def _scrape_tcg_inventory(session, config, db, logger) -> None:
    """Browser-scrape every TCG product into the local inventory table.

    Shared core of `sync` and `sync-inventory`. Caller owns session + db
    lifecycle. Prints progress to the global console.
    """
    from scryland.browser.pages.inventory import InventoryPage
    from scryland.models import Listing

    inventory_page = InventoryPage(session.page, config)
    await inventory_page.navigate()

    products = await inventory_page.get_product_names()
    console.print(f"Found [cyan]{len(products)}[/cyan] products on TCGPlayer.")

    all_listings: list[Listing] = []
    for idx, product in enumerate(products):
        product_name = product["name"]
        console.print(
            f"  Scraping ({idx + 1}/{len(products)}): {product_name}",
            end="",
        )

        await session.human_delay()
        await session.dismiss_popups()

        try:
            await inventory_page.click_manage_for_product(product_name)
            await session.dismiss_popups()
            listings = await inventory_page.get_manage_page_listings(product_name)
            all_listings.extend(listings)
            console.print(f" — [green]{len(listings)} listings[/green]")
        except Exception:
            console.print(" — [red]failed[/red]")
            logger.debug("Failed to scrape '%s'", product_name, exc_info=True)

        await session.human_delay()
        # reapply_filter=True is required: TCG drops 'My Inventory Only'
        # across the manage→Back-to-Inventory redirect. Without this,
        # iterations 2+ paginate the global catalog instead of the user's
        # listings — same gotcha already documented in _tcg_floor_sweep.
        await inventory_page.go_back_to_inventory(reapply_filter=True)

    console.print(f"\nSyncing {len(all_listings)} listings to database...")
    report = db.sync(all_listings)
    summary = db.get_summary()
    _print_sync_report(report, summary)


async def _refresh_ebay_listings(config, db, logger) -> dict:
    """Refresh ebay_listings rows from the live Sell API.

    Walks every active offer via `/sell/inventory/v1/offer`, updates the
    matching local row's price/quantity/status. New offers (not in our DB)
    are skipped and reported — they should be re-created via list-on-ebay
    so canonical_key gets reconstructed properly from inventory_item.

    Returns counts: {updated, missing_local, total_remote}.
    """
    from scryland.ebay.auth import EbayAuth
    from scryland.ebay.client import EbayClient

    passphrase = _ebay_passphrase(config)
    auth = EbayAuth(config)
    try:
        await auth.access_token(passphrase)
    except Exception as exc:
        console.print(f"[red]eBay auth failed: {exc}[/red]")
        return {"updated": 0, "missing_local": 0, "total_remote": 0, "error": str(exc)}

    by_sku = {
        row["sku"]: row
        for row in db.conn.execute(
            "SELECT sku, product_name, set_name, collector_number, condition, "
            "is_foil FROM ebay_listings"
        ).fetchall()
    }
    skus = list(by_sku.keys())
    if not skus:
        console.print("  [dim]No local eBay listings to refresh.[/dim]")
        return {"updated": 0, "missing_local": 0, "total_remote": 0}

    console.print(f"Fetching offers for {len(skus)} local SKU(s)…")
    # Client only needed for the offer fetch; DB writes below don't need
    # it. If a future change adds per-row API calls inside the loop,
    # the client will already be closed — move that work inside this block.
    async with EbayClient(config, auth, passphrase) as client:
        offers = await client.iter_offers_for_skus(skus)

    console.print(f"Fetched [cyan]{len(offers)}[/cyan] offer(s) from eBay.")

    updated = 0
    missing_local: list[str] = []
    for offer in offers:
        sku = offer.get("sku")
        if not sku:
            continue
        local = by_sku.get(sku)
        if local is None:
            missing_local.append(sku)
            continue
        # Map offer status to our local enum. Published/active offers stay
        # 'active'; withdrawn/ended become 'ended'.
        status_remote = (offer.get("status") or "").upper()
        if status_remote in {"PUBLISHED", "ACTIVE"}:
            status_local = "active"
        elif status_remote in {"UNPUBLISHED"}:
            status_local = "draft"
        else:
            status_local = "ended"
        price_str = (
            offer.get("pricingSummary", {}).get("price", {}).get("value")
            if offer.get("pricingSummary")
            else None
        )
        try:
            price = float(price_str) if price_str else 0.0
        except (TypeError, ValueError):
            price = 0.0
        quantity = int(offer.get("availableQuantity") or 0)
        listing_id = offer.get("listing", {}).get("listingId") if offer.get("listing") else None
        db.upsert_ebay_listing(
            sku=sku,
            offer_id=offer.get("offerId"),
            listing_id=listing_id,
            product_name=local["product_name"],
            set_name=local["set_name"],
            collector_number=local["collector_number"],
            condition=local["condition"],
            is_foil=bool(local["is_foil"]),
            price=price,
            quantity=quantity,
            status=status_local,
        )
        updated += 1
    db.conn.commit()

    if missing_local:
        console.print(
            f"  [yellow]{len(missing_local)} eBay offer(s) have no local row[/yellow] "
            f"— they were created outside scryland. Re-run list-on-ebay against "
            f"your CSV to backfill canonical_key for these."
        )
        for sku in missing_local[:5]:
            console.print(f"    [dim]{sku}[/dim]")
        if len(missing_local) > 5:
            console.print(f"    [dim]... and {len(missing_local) - 5} more[/dim]")

    return {
        "updated": updated,
        "missing_local": len(missing_local),
        "total_remote": len(offers),
    }


@cli.command()
@click.pass_context
def sync(ctx: click.Context) -> None:
    """Scrape all TCGPlayer listings and sync to the local DB.

    Opens a browser, iterates every product, and records each listing's
    current price / qty / tcg-lowest. Items missing from the scrape are
    marked 'removed' (not 'sold' — true sold status comes from recording
    an actual order via `sales` or `watch`).
    """
    config: ScrylandConfig = ctx.obj["config"]
    logger = ctx.obj["logger"]

    async def _run() -> None:
        from scryland.browser.session import BrowserSession
        from scryland.db import InventoryDB

        session = BrowserSession(config)
        db = InventoryDB(Path(config.db_path))

        try:
            db.open()
            await session.start()
            await session.ensure_logged_in()
            await _scrape_tcg_inventory(session, config, db, logger)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception:
            logger.exception("Sync failed")
            sys.exit(1)
        finally:
            db.close()
            try:
                await session.close()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")


@cli.command("sync-inventory")
@click.option("--tcg-only", is_flag=True, help="Only refresh TCG inventory (skip eBay).")
@click.option("--ebay-only", is_flag=True, help="Only refresh eBay listings (skip TCG).")
@click.pass_context
def sync_inventory(ctx: click.Context, tcg_only: bool, ebay_only: bool) -> None:
    """Refresh both local inventory tables from live marketplace data.

    - TCG: browser-walk every product, scrape its manage page (slow,
      ~5–10s per product).
    - eBay: paginate /sell/inventory/v1/offer and update local rows
      (fast, seconds for hundreds).

    Use this after listings have been added/edited outside scryland, or
    when the uncompetitive-delist sweep is missing rows because a local
    canonical_key has no matching TCG inventory entry.

    --tcg-only / --ebay-only run just one side. By default both run.
    """
    if tcg_only and ebay_only:
        console.print("[red]--tcg-only and --ebay-only are mutually exclusive.[/red]")
        sys.exit(1)
    config: ScrylandConfig = ctx.obj["config"]
    logger = ctx.obj["logger"]

    async def _run() -> None:
        from scryland.browser.session import BrowserSession
        from scryland.db import InventoryDB

        db = InventoryDB(Path(config.db_path))
        session: BrowserSession | None = None
        try:
            db.open()

            if not ebay_only:
                console.print("[bold]TCG sync[/bold] (browser scrape)…")
                session = BrowserSession(config)
                await session.start()
                await session.ensure_logged_in()
                await _scrape_tcg_inventory(session, config, db, logger)

            if not tcg_only:
                console.print("\n[bold]eBay sync[/bold] (Sell API)…")
                result = await _refresh_ebay_listings(config, db, logger)
                console.print(
                    f"  Updated [green]{result['updated']}[/green] eBay row(s) "
                    f"from {result['total_remote']} live offer(s)."
                )
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception:
            logger.exception("sync-inventory failed")
            sys.exit(1)
        finally:
            db.close()
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")


def _print_sync_report(report, summary: dict | None = None) -> None:
    """Print a sync report with changes."""
    console.print()
    t = Table(title="Sync Complete", show_header=False, box=None, padding=(0, 2))
    t.add_column("Label", style="bold")
    t.add_column("Value")
    t.add_row("Active listings", f"[cyan]{report.total_active}[/cyan]")
    if summary:
        t.add_row("Total quantity", f"[cyan]{summary['total_quantity']}[/cyan]")
        t.add_row("Total value", f"[green]${summary['total_value']:.2f}[/green]")
        if summary["sold_count"] > 0:
            t.add_row("Sold/removed", f"[red]{summary['sold_count']}[/red]")
    console.print(t)

    if not report.has_changes:
        console.print("\n  [green]No changes detected.[/green]")
        return

    if report.added:
        added_table = Table(title=f"New Listings ({len(report.added)})", style="green")
        added_table.add_column("Card")
        for name in report.added:
            added_table.add_row(name)
        console.print(added_table)

    if report.removed:
        removed_table = Table(title=f"Sold/Removed ({len(report.removed)})", style="red")
        removed_table.add_column("Card")
        for name in report.removed:
            removed_table.add_row(name)
        console.print(removed_table)

    if report.price_changed:
        price_table = Table(title=f"Price Changes ({len(report.price_changed)})")
        price_table.add_column("Card", style="cyan")
        price_table.add_column("Old Price", justify="right", style="red")
        price_table.add_column("New Price", justify="right", style="green")
        for change in report.price_changed:
            price_table.add_row(
                change["name"],
                f"${change['old_price']:.2f}",
                f"${change['new_price']:.2f}",
            )
        console.print(price_table)

    if report.quantity_changed:
        qty_table = Table(title=f"Quantity Changes ({len(report.quantity_changed)})")
        qty_table.add_column("Card", style="cyan")
        qty_table.add_column("Old Qty", justify="right")
        qty_table.add_column("New Qty", justify="right")
        for change in report.quantity_changed:
            qty_table.add_row(
                change["name"],
                str(change["old_qty"]),
                str(change["new_qty"]),
            )
        console.print(qty_table)


@cli.command()
@click.pass_context
def sales(ctx: click.Context) -> None:
    """Scrape TCGPlayer orders and record sales to the local DB.

    Atomic per-order: a partial scrape failure leaves the whole order
    unrecorded so the next run can retry.

    After recording sales, automatically withdraws any matching active eBay
    offers (cross_delist_done=0 rows) via the Sell API — same logic as the
    watch loop's cross-delist step. Safe to run standalone after a TCG sale
    to sync both marketplaces without waiting for the next watch cycle.

    For eBay sales, use `ebay-sync` instead.
    """
    config: ScrylandConfig = ctx.obj["config"]
    logger = ctx.obj["logger"]

    async def _run() -> None:
        from scryland.browser.pages.orders import OrdersPage
        from scryland.browser.session import BrowserSession
        from scryland.db import InventoryDB

        session = BrowserSession(config)
        db = InventoryDB(Path(config.db_path))

        try:
            db.open()
            await session.start()
            await session.ensure_logged_in()

            orders_page = OrdersPage(session.page, config)
            await orders_page.navigate()
            await session.dismiss_popups()

            # Get known orders to skip already-recorded ones
            known_orders = db.get_known_order_numbers()
            console.print(f"Already have [cyan]{len(known_orders)}[/cyan] orders in database.")

            # Scrape order list
            order_rows = await orders_page.get_order_rows()
            console.print(f"Found [cyan]{len(order_rows)}[/cyan] orders on TCGPlayer.")

            new_sales = 0
            for idx, order in enumerate(order_rows):
                order_num = order.get("order_number", "")
                if order_num in known_orders:
                    console.print(
                        f"  ({idx + 1}/{len(order_rows)}) {order_num} — [dim]already recorded[/dim]"
                    )
                    continue

                console.print(
                    f"  ({idx + 1}/{len(order_rows)}) {order_num} — scraping details...",
                    end="",
                )

                await session.human_delay()

                try:
                    href = order.get("order_href", "")
                    if not href:
                        console.print(" [red]no link[/red]")
                        continue

                    details = await orders_page.get_order_details(href)
                    # Fill in fallbacks from the order-list row
                    details.setdefault("order_date", order.get("order_date", ""))
                    details.setdefault("buyer_name", order.get("buyer_name", ""))
                    details.setdefault("status", order.get("status", ""))

                    sales_for_order = _build_sales_rows(details, order_num)
                    new_sales += db.record_order_sales(sales_for_order)

                    console.print(
                        f" [green]{len(sales_for_order)} item(s), "
                        f"${details.get('net_amount', 0):.2f} net[/green]"
                    )

                    await orders_page.go_back_to_orders()

                except Exception:
                    console.print(" [red]failed[/red]")
                    logger.warning(
                        "Failed to scrape order %s — left unrecorded",
                        order_num,
                        exc_info=True,
                    )
                    try:
                        await orders_page.navigate()
                    except Exception:
                        pass

            # Print summary
            console.print(f"\n[bold]Sales sync complete.[/bold] {new_sales} new sale(s) recorded.")

            if new_sales > 0:
                summary = db.get_sales_summary()
                console.print(f"  Total orders:     [cyan]{summary['total_orders']}[/cyan]")
                console.print(f"  Total items sold: [cyan]{summary['total_items_sold']}[/cyan]")
                console.print(f"  Total revenue:    [green]${summary['total_revenue']:.2f}[/green]")
                console.print(f"  Total fees:       [red]${summary['total_fees']:.2f}[/red]")
                console.print(f"  Net income:       [green]${summary['total_net']:.2f}[/green]")

            # Cross-delist on eBay for any pending TCG sales.
            if config.ebay_app_id:
                from scryland.db import canonical_key as _canonical_key
                from scryland.ebay.auth import EbayAuth
                from scryland.ebay.client import EbayClient

                console.print("\n[bold]eBay cross-delist check…[/bold]")
                try:
                    passphrase = _ebay_passphrase(config)
                    auth = EbayAuth(config)
                    await auth.access_token(passphrase)
                    pending = db.conn.execute(
                        "SELECT id, product_name, condition FROM sales "
                        "WHERE (marketplace = 'tcgplayer' OR marketplace IS NULL) "
                        "AND (cross_delist_done IS NULL OR cross_delist_done = 0) "
                        "ORDER BY id DESC LIMIT 200"
                    ).fetchall()
                    withdrawn = 0
                    async with EbayClient(config, auth, passphrase) as client:
                        for row in pending:
                            any_failure = False
                            for foil in (False, True):
                                key = _canonical_key(row["product_name"], row["condition"], foil)
                                match = db.find_ebay_listing_by_canonical(key)
                                if not match or not match.get("offer_id"):
                                    continue
                                if await client.withdraw_offer(match["offer_id"]):
                                    db.mark_ebay_listing_status(match["sku"], "ended")
                                    console.print(
                                        f"  [yellow]eBay delist: '{match['product_name']}'"
                                        f" — sold on TCG[/yellow]"
                                    )
                                    withdrawn += 1
                                else:
                                    any_failure = True
                                    console.print(
                                        f"  [red]eBay delist FAILED for '{match['product_name']}'"
                                        f" — will retry next run[/red]"
                                    )
                            if not any_failure:
                                db.conn.execute(
                                    "UPDATE sales SET cross_delist_done = 1 WHERE id = ?",
                                    (row["id"],),
                                )
                        if pending:
                            db.conn.commit()
                    if withdrawn:
                        console.print(f"[green]Withdrew {withdrawn} eBay listing(s).[/green]")
                    elif pending:
                        console.print("  [dim]No matching eBay listings found.[/dim]")
                    else:
                        console.print("  [dim]No pending TCG sales to cross-delist.[/dim]")
                except Exception:
                    console.print(
                        "  [yellow]eBay cross-delist skipped — auth unavailable.[/yellow]"
                    )
                    logger.warning("eBay cross-delist failed", exc_info=True)

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception:
            logger.exception("Sales sync failed")
            sys.exit(1)
        finally:
            db.close()
            try:
                await session.close()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")


@cli.command("price-history")
@click.option("--card", "-c", type=str, default=None, help="Show history for a specific card")
@click.pass_context
def price_history(ctx: click.Context, card: str | None) -> None:
    """Show historical price data for an inventory item.

    Uses the price_history table populated by every sync.
    """
    config: ScrylandConfig = ctx.obj["config"]

    from scryland.db import InventoryDB

    db = InventoryDB(Path(config.db_path))
    db.open()

    try:
        if card:
            # Show history for a specific card
            history = db.get_price_history(card)
            if not history:
                console.print(f"[yellow]No price history for '{card}'.[/yellow]")
                console.print("Try a partial name — searching all history...")
                # Fuzzy search
                rows = db.conn.execute(
                    "SELECT DISTINCT product_name, condition FROM price_history "
                    "WHERE product_name LIKE ? ORDER BY product_name",
                    (f"%{card}%",),
                ).fetchall()
                if rows:
                    for row in rows:
                        console.print(f"  - {row['product_name']} ({row['condition']})")
                    return
                console.print("[yellow]No matches found.[/yellow]")
                return

            t = Table(title=f"Price History: {card}")
            t.add_column("Date")
            t.add_column("MP")
            t.add_column("Condition")
            t.add_column("Our Price", justify="right", style="green")
            t.add_column("TCG Low / eBay Low", justify="right")
            t.add_column("Market", justify="right")

            for h in history:
                date = h["recorded_at"][:16].replace("T", " ") if h["recorded_at"] else "-"
                our = f"${h['our_price']:.2f}" if h["our_price"] else "-"
                low = f"${h['tcg_low']:.2f}" if h["tcg_low"] else "-"
                market = f"${h['market_price']:.2f}" if h["market_price"] else "-"
                mp = h["marketplace"] if "marketplace" in h.keys() else "tcgplayer"
                mp_cell = "[cyan]TCG[/cyan]" if mp == "tcgplayer" else "[magenta]EBAY[/magenta]"
                t.add_row(date, mp_cell, h["condition"], our, low, market)
            console.print(t)
        else:
            # Show price extremes for all cards
            extremes = db.get_price_extremes()
            if not extremes:
                console.print(
                    "[yellow]No price history recorded yet. Run [cyan]scryland sync[/cyan] or [cyan]scryland optimize[/cyan] first.[/yellow]"
                )
                return

            t = Table(title="Price History — All Cards")
            t.add_column("Product", style="cyan")
            t.add_column("Condition")
            t.add_column("Current", justify="right", style="green")
            t.add_column("Low", justify="right")
            t.add_column("High", justify="right")
            t.add_column("vs Low", justify="right")
            t.add_column("vs High", justify="right")
            t.add_column("Pts", justify="right")

            for e in extremes:
                current = e["current_price"]
                lowest = e["lowest_seen"]
                highest = e["highest_seen"]

                cur_str = f"${current:.2f}" if current else "-"
                low_str = f"${lowest:.2f}" if lowest else "-"
                high_str = f"${highest:.2f}" if highest else "-"

                # % vs low (how much above the lowest it's been)
                if current and lowest and lowest > 0:
                    pct_vs_low = ((current - lowest) / lowest) * 100
                    if pct_vs_low > 0:
                        vs_low = f"[green]+{pct_vs_low:.0f}%[/green]"
                    elif pct_vs_low < 0:
                        vs_low = f"[red]{pct_vs_low:.0f}%[/red]"
                    else:
                        vs_low = "[dim]0%[/dim]"
                else:
                    vs_low = "-"

                # % vs high (how much below the highest it's been)
                if current and highest and highest > 0:
                    pct_vs_high = ((current - highest) / highest) * 100
                    if pct_vs_high < 0:
                        vs_high = f"[red]{pct_vs_high:.0f}%[/red]"
                    elif pct_vs_high > 0:
                        vs_high = f"[green]+{pct_vs_high:.0f}%[/green]"
                    else:
                        vs_high = "[dim]0%[/dim]"
                else:
                    vs_high = "-"

                t.add_row(
                    e["product_name"],
                    e["condition"],
                    cur_str,
                    low_str,
                    high_str,
                    vs_low,
                    vs_high,
                    str(e["data_points"]),
                )
            console.print(t)
    finally:
        db.close()


@cli.command("sales-report")
@click.pass_context
def sales_report(ctx: click.Context) -> None:
    """Show recorded sales (TCG + eBay) with totals. No browser needed.

    Reads from the local DB. Run `sales` (TCG) or `ebay-sync` (eBay) to
    populate it first.
    """
    config: ScrylandConfig = ctx.obj["config"]

    from scryland.db import InventoryDB

    db = InventoryDB(Path(config.db_path))
    db.open()

    try:
        all_sales = db.get_all_sales()
        summary = db.get_sales_summary()

        if not all_sales:
            console.print(
                "[yellow]No sales recorded. Run [cyan]scryland sales[/cyan] first.[/yellow]"
            )
            return

        # Summary stats — combined + per marketplace when both exist.
        console.print()
        per_mp = db.get_sales_summary_by_marketplace()
        has_both = len(per_mp) > 1
        st = Table(title="Sales Summary", show_header=has_both, header_style="bold")
        st.add_column("Label", style="bold")
        if has_both:
            for mp in per_mp:
                st.add_column(mp["marketplace"].upper(), justify="right")
        st.add_column("All", justify="right")

        def _fmt_row(label: str, key: str, fmt: str) -> None:
            values = [fmt.format(mp[key]) for mp in per_mp] if has_both else []
            values.append(fmt.format(summary[key]))
            st.add_row(label, *values)

        _fmt_row("Orders", "total_orders", "{}")
        _fmt_row("Items sold", "total_items_sold", "{}")
        _fmt_row("Revenue", "total_revenue", "${:.2f}")
        _fmt_row("Fees", "total_fees", "${:.2f}")
        _fmt_row("Net", "total_net", "${:.2f}")
        _fmt_row("Avg sale", "avg_sale_price", "${:.2f}")
        console.print(st)

        # Sales table
        t = Table(title="\nSales History")
        t.add_column("Date")
        t.add_column("MP")
        t.add_column("Product", style="cyan")
        t.add_column("Condition")
        t.add_column("Qty", justify="right")
        t.add_column("Price", justify="right", style="green")
        t.add_column("Net", justify="right")
        t.add_column("Buyer")

        total_revenue = 0.0
        total_net = 0.0
        total_qty = 0
        for sale in all_sales:
            date = sale["order_date"][:10] if sale["order_date"] else "-"
            price = f"${sale['sale_price']:.2f}" if sale["sale_price"] else "-"
            net = f"${sale['net_amount']:.2f}" if sale["net_amount"] else "-"
            mp = sale["marketplace"] if "marketplace" in sale.keys() else "tcgplayer"
            mp_cell = "[cyan]TCG[/cyan]" if mp == "tcgplayer" else "[magenta]EBAY[/magenta]"
            total_revenue += (sale["sale_price"] or 0) * (sale["quantity"] or 1)
            total_net += sale["net_amount"] or 0
            total_qty += sale["quantity"] or 0

            t.add_row(
                date,
                mp_cell,
                sale["product_name"],
                sale["condition"],
                str(sale["quantity"]),
                price,
                net,
                sale["buyer_name"],
            )

        t.add_section()
        t.add_row(
            "[bold]Totals[/bold]",
            "",
            "",
            "",
            f"[bold]{total_qty}[/bold]",
            f"[bold]${total_revenue:.2f}[/bold]",
            f"[bold]${total_net:.2f}[/bold]",
            "",
        )
        console.print(t)

    finally:
        db.close()


@cli.command()
@click.option(
    "--interval", "-i", type=int, default=60, help="Minutes between optimize runs (default: 60)"
)
@click.option(
    "--volatile",
    is_flag=True,
    default=False,
    help="Skip the 10%% change confirmation — auto-approve all price changes",
)
@click.option(
    "--delist-below",
    type=float,
    default=0.01,
    help="End TCG listings whose price is at or below this value. "
    "Default: $0.01 (only true penny listings). Pass 0 to disable.",
)
@click.option(
    "--ebay/--no-ebay",
    default=True,
    help="Also sweep eBay prices (undercut) and sales each run. Default: on.",
)
@click.option(
    "--ebay-only",
    is_flag=True,
    default=False,
    help="Skip TCG entirely. No browser. Just eBay price undercut "
    "and eBay sales — runs via API only.",
)
@click.option(
    "--ebay-min-price",
    type=float,
    default=0.99,
    help="Floor for eBay undercut pricing. Note: eBay enforces a "
    "hard $0.99 minimum; values below will be clamped. Default: $0.99",
)
@click.option(
    "--ebay-max-price",
    type=float,
    default=None,
    help="Skip eBay undercut for listings priced above this. Default: no ceiling.",
)
@click.option(
    "--ebay-delist-below",
    type=float,
    default=None,
    help="Withdraw eBay listings where the competitor lowest drops "
    "below this price (instead of undercutting to the floor).",
)
@click.option(
    "--ebay-delist-uncompetitive-gap",
    type=float,
    default=None,
    help="Withdraw eBay listings where our price is more than this many "
    "dollars above the matching TCG listing's price (assumes similar "
    "shipping). Use 0.50 to delist when ~$0.50+ over TCG. Off by default.",
)
@click.option(
    "--tcg-refresh-days",
    type=float,
    default=3.0,
    show_default=True,
    help="Days between full TCG inventory scrapes within watch. Pass 0 to disable.",
)
@click.option(
    "--prompt-timeout",
    type=float,
    default=30.0,
    help="When --volatile is OFF, auto-default the 'apply big drop?' prompt "
    "after this many seconds so unattended watch runs progress instead of "
    "stalling. Default 30s; pass 0 to wait forever.",
)
@click.pass_context
def watch(
    ctx: click.Context,
    interval: int,
    volatile: bool,
    delist_below: float,
    ebay: bool,
    ebay_only: bool,
    ebay_min_price: float,
    ebay_max_price: float | None,
    ebay_delist_below: float | None,
    ebay_delist_uncompetitive_gap: float | None,
    tcg_refresh_days: float,
    prompt_timeout: float,
) -> None:
    """Recurring multi-marketplace price optimizer + sales watcher.

    \b
    Each iteration does (unless --ebay-only):
      1. TCG: periodic full inventory scrape every --tcg-refresh-days days
         (default 3) to keep current_price fresh.
      2. TCG: Price Differential Report → apply updates / delists.
      3. TCG: floor sweep — end listings priced at/below --delist-below
         (default $0.01; pass 0 to disable).
      4. TCG: check orders for new sales → record to DB.
    Then always (unless --no-ebay):
      5. eBay: fetch recent orders → record sales → mark our listings sold
         → cross-delist matching TCG listings (via the open browser).
      6. eBay: withdraw eBay offers for any TCG sale not yet cross-delisted.
      7. eBay: Browse-API undercut sweep — bump prices to (lowest-$0.01),
         floored by --ebay-min-price, ceilinged by --ebay-max-price.
      8. eBay: if --ebay-delist-below set, end listings when market drops
         below that threshold.

    --ebay-only skips the browser entirely — all API, no TCG scraping.
    Great for fast/quiet price maintenance. Pair with a less-frequent
    full run to cover TCG.
    """
    config: ScrylandConfig = ctx.obj["config"]
    if volatile:
        config = config.model_copy(update={"max_price_change_pct": 999.0})
        console.print("[yellow]Volatile mode — all price changes will be auto-approved.[/yellow]")
    # Propagate the prompt timeout so the optimizer's confirm helper picks
    # it up — without volatile, big drops still need a y/N, but unattended
    # runs auto-skip after the timeout instead of stalling.
    config = config.model_copy(update={"prompt_timeout_s": prompt_timeout})
    logger = ctx.obj["logger"]

    async def _run() -> None:
        import time
        from datetime import datetime

        from scryland.browser.pages.orders import OrdersPage
        from scryland.browser.session import BrowserSession
        from scryland.db import InventoryDB
        from scryland.pricing.optimizer import (
            OptimizeResult,
            run_price_differential_optimize,
        )

        session = BrowserSession(config) if not ebay_only else None
        db = InventoryDB(Path(config.db_path))

        try:
            db.open()
            if session is not None:
                await session.start()
                await session.ensure_logged_in()

            mode = "eBay-only (API)" if ebay_only else "TCG + eBay"
            console.print(
                f"\n[bold]Watching prices every {interval} minutes "
                f"in {mode} mode. Press Ctrl+C to stop.[/bold]"
            )

            run_count = 0
            # Cumulative stats across iterations — printed in each summary.
            cumulative = {
                "ebay_updates": 0,
                "ebay_delists": 0,
                "ebay_withdraws_tcg_sold": 0,
                "ebay_total_change": 0.0,
                "tcg_updates": 0,
                "tcg_total_change": 0.0,
                "new_sales": 0,
                "started_at": datetime.now(),
            }
            while True:
                run_count += 1
                now = datetime.now().strftime("%I:%M %p")
                console.print(
                    f"\n[bold]═══ Run #{run_count} at {now} (every {interval}m) ═══[/bold]"
                )

                # Recreate a dead browser session (e.g. crash, network reset,
                # TargetClosedError from a prior run) before doing any TCG work.
                if not ebay_only and (session is None or not session.is_alive()):
                    if session is not None:
                        console.print("[yellow]Browser session lost — restarting...[/yellow]")
                        try:
                            await session.close()
                        except Exception:
                            pass
                    session = BrowserSession(config)
                    try:
                        await session.start()
                        await session.ensure_logged_in()
                    except Exception:
                        logger.exception("Failed to restart browser session")
                        console.print(
                            "[red]Could not restart browser — skipping TCG this run[/red]"
                        )
                        # The half-initialized session would crash every
                        # downstream call (run_price_differential_optimize,
                        # OrdersPage(session.page, ...)). Drop the reference
                        # so this iteration runs eBay-only and the next
                        # iteration retries the restart from scratch.
                        try:
                            await session.close()
                        except Exception:
                            pass
                        session = None

                opt_result = OptimizeResult()
                total_change = 0.0
                # Initialize so the post-loop detail-table code doesn't
                # UnboundLocalError if the try-block raises early.
                ebay_result: dict = _empty_ebay_result()
                try:
                    if ebay_only or session is None:
                        # eBay-only (or browser restart failed): skip the
                        # TCG optimize + TCG sales scrape.
                        pass
                    else:
                        # Periodic full TCG inventory scrape — keeps current_price
                        # fresh for the eBay uncompetitive-gap delist check.
                        if tcg_refresh_days > 0:
                            from datetime import datetime

                            last_str = db.get_metadata("last_tcg_scrape")
                            last_dt = datetime.fromisoformat(last_str) if last_str else None
                            overdue = (
                                last_dt is None
                                or (datetime.now() - last_dt).total_seconds()
                                > tcg_refresh_days * 86400
                            )
                            if overdue:
                                console.print(
                                    f"\n[bold]TCG inventory refresh[/bold]"
                                    f" [dim](every {tcg_refresh_days:.0f}d)[/dim]"
                                )
                                try:
                                    await _scrape_tcg_inventory(session, config, db, logger)
                                    db.set_metadata("last_tcg_scrape", datetime.now().isoformat())
                                except Exception:
                                    logger.warning("Periodic TCG scrape failed", exc_info=True)
                                    console.print(
                                        "[yellow]TCG inventory refresh failed — continuing[/yellow]"
                                    )

                        opt_result = await run_price_differential_optimize(
                            session, config, console, db=db
                        )
                    total_change = opt_result.total_change
                    if opt_result.total:
                        direction = "[green]up[/green]" if total_change > 0 else "[red]down[/red]"
                        console.print(
                            f"Updated {opt_result.updated}/{opt_result.total} listings "
                            f"(delisted {opt_result.delisted}). "
                            f"Net change: {direction} ${abs(total_change):.2f}"
                        )

                    # TCG floor sweep — covers the gap where a card has
                    # drifted to the bottom of market and stops appearing
                    # in the differential report (no diff = no row).
                    if not ebay_only and session is not None and delist_below and delist_below > 0:
                        try:
                            floor_delisted = await _tcg_floor_sweep(
                                session, config, db, logger, delist_below
                            )
                            opt_result.delisted += floor_delisted
                        except Exception:
                            logger.warning("TCG floor sweep failed", exc_info=True)
                            console.print("[yellow]TCG floor sweep skipped — see log[/yellow]")

                    # Quick TCG sales check — only runs when we have a browser.
                    if not ebay_only and session is not None:
                        try:
                            orders_page = OrdersPage(session.page, config)
                            await orders_page.navigate()
                            await session.dismiss_popups()
                            known = db.get_known_order_numbers()
                            rows = await orders_page.get_order_rows()
                            new_orders = [o for o in rows if o.get("order_number") not in known]

                            if not new_orders:
                                console.print("No new sales.")
                            else:
                                console.print(f"[cyan]{len(new_orders)}[/cyan] new order(s) found!")
                                new_sales = 0
                                for order in new_orders:
                                    order_num = order.get("order_number", "")
                                    href = order.get("order_href", "")
                                    if not href:
                                        continue
                                    await session.human_delay()
                                    try:
                                        details = await orders_page.get_order_details(href)
                                        sales_for_order = _build_sales_rows(details, order_num)
                                        new_sales += db.record_order_sales(sales_for_order)
                                        await orders_page.go_back_to_orders()
                                    except Exception:
                                        logger.warning(
                                            "Failed to scrape order %s — left unrecorded",
                                            order_num,
                                            exc_info=True,
                                        )
                                if new_sales:
                                    summary = db.get_sales_summary()
                                    console.print(
                                        f"[green]{new_sales} new sale(s)! "
                                        f"Net income: ${summary['total_net']:.2f}[/green]"
                                    )
                        except Exception:
                            logger.warning("Sales check failed", exc_info=True)
                            console.print("[yellow]Sales check skipped — see log[/yellow]")

                    # ---- eBay sweep: undercut prices + sales check +
                    # (if browser is open) cross-marketplace TCG delist.
                    # Runs in eBay-only mode too — no browser needed for the
                    # API-side work.
                    if (ebay or ebay_only) and config.ebay_app_id:
                        try:
                            ebay_result = (
                                await _ebay_watch_pass(
                                    config,
                                    db,
                                    session,
                                    logger,
                                    min_price=ebay_min_price,
                                    max_price=ebay_max_price,
                                    delist_below=ebay_delist_below,
                                    delist_uncompetitive_gap=ebay_delist_uncompetitive_gap,
                                )
                                or {}
                            )
                        except Exception:
                            logger.warning("eBay watch pass failed", exc_info=True)
                            console.print("[yellow]eBay sweep skipped — see log[/yellow]")

                except Exception:
                    logger.exception("Run #%d failed", run_count)

                # Run summary
                try:
                    inv_summary = db.get_summary()

                    # Accumulate cumulative stats.
                    # Count *applied* updates, not differentials found.
                    cumulative["tcg_updates"] += opt_result.updated or 0
                    cumulative["tcg_total_change"] += total_change
                    cumulative["ebay_updates"] += ebay_result.get("updated", 0)
                    cumulative["ebay_delists"] += ebay_result.get("delisted", 0)
                    cumulative["ebay_withdraws_tcg_sold"] += ebay_result.get("withdrawn", 0)
                    cumulative["new_sales"] += ebay_result.get("new_sales", 0)
                    ebay_run_change = sum(
                        c["new"] - c["old"] for c in ebay_result.get("changes") or []
                    )
                    cumulative["ebay_total_change"] += ebay_run_change

                    console.print()
                    st = Table(
                        title=f"Run #{run_count} Summary",
                        show_header=False,
                        box=None,
                        padding=(0, 2),
                    )
                    st.add_column("Label", style="bold")
                    st.add_column("Value")
                    st.add_row("Active listings", f"[cyan]{inv_summary['active_listings']}[/cyan]")
                    st.add_row(
                        "Inventory value", f"[green]${inv_summary['total_value']:.2f}[/green]"
                    )
                    # Show price changes this run — TCG side
                    if opt_result.total:
                        changes_str = f"{opt_result.total} price change(s)"
                        if total_change != 0:
                            sign = "+" if total_change > 0 else "-"
                            changes_str += f" ({sign}${abs(total_change):.2f})"
                        st.add_row("TCG this run", changes_str)
                    elif not ebay_only and session is None:
                        st.add_row(
                            "TCG this run",
                            "[yellow]Skipped — browser unavailable[/yellow]",
                        )
                    elif not ebay_only:
                        st.add_row("TCG this run", "[green]No changes needed[/green]")
                    # eBay side
                    if ebay_result.get("error"):
                        st.add_row(
                            "eBay this run",
                            f"[red]error: {ebay_result['error']}[/red]",
                        )
                    elif ebay_result.get("checked"):
                        parts = [f"{ebay_result['updated']}/{ebay_result['checked']} updated"]
                        if ebay_result.get("delisted"):
                            parts.append(f"{ebay_result['delisted']} delisted")
                        if ebay_result.get("withdrawn"):
                            parts.append(f"{ebay_result['withdrawn']} withdrawn")
                        if ebay_result.get("new_sales"):
                            parts.append(f"{ebay_result['new_sales']} sale(s)")
                        if ebay_result.get("browse_errors"):
                            parts.append(
                                f"[yellow]{ebay_result['browse_errors']} browse errs[/yellow]"
                            )
                        if ebay_result.get("tcg_delist_failed"):
                            parts.append(
                                f"[red]{ebay_result['tcg_delist_failed']} TCG delist fail[/red]"
                            )
                        if ebay_result.get("skipped_big_drops"):
                            parts.append(
                                f"[yellow]{ebay_result['skipped_big_drops']} skipped (>{config.max_price_change_pct:.0f}%)[/yellow]"
                            )
                        st.add_row("eBay this run", ", ".join(parts))
                    elif ebay or ebay_only:
                        st.add_row("eBay this run", "[green]No changes needed[/green]")
                    # Cumulative across all runs since watch started.
                    elapsed = datetime.now() - cumulative["started_at"]
                    hours = elapsed.total_seconds() / 3600
                    cum_parts = []
                    if cumulative["tcg_updates"] or cumulative["ebay_updates"]:
                        total_updates = cumulative["tcg_updates"] + cumulative["ebay_updates"]
                        net_change = (
                            cumulative["tcg_total_change"] + cumulative["ebay_total_change"]
                        )
                        color = "green" if net_change >= 0 else "red"
                        sign = "+" if net_change >= 0 else "-"
                        cum_parts.append(
                            f"{total_updates} updates, "
                            f"[{color}]{sign}${abs(net_change):.2f}[/{color}]"
                        )
                    if cumulative["ebay_delists"]:
                        cum_parts.append(f"{cumulative['ebay_delists']} delist(s)")
                    if cumulative["new_sales"]:
                        cum_parts.append(f"{cumulative['new_sales']} sale(s)")
                    if cum_parts:
                        st.add_row(
                            f"Cumulative ({hours:.1f}h, {run_count} runs)",
                            ", ".join(cum_parts),
                        )
                    console.print(st)
                except Exception:
                    pass

                # Detailed per-item change table for eBay price updates.
                changes = (ebay_result or {}).get("changes") or []
                delisted_items = (ebay_result or {}).get("delisted_items") or []
                if changes or delisted_items:
                    detail = Table(
                        title="eBay Price Changes",
                        show_header=True,
                        header_style="bold",
                        padding=(0, 1),
                    )
                    detail.add_column("Card", style="cyan", max_width=40)
                    detail.add_column("Old", justify="right")
                    detail.add_column("New", justify="right")
                    detail.add_column("Δ $", justify="right")
                    detail.add_column("Δ %", justify="right")
                    detail.add_column("Direction")

                    # Sort: biggest $ move first so the headline changes pop.
                    sorted_changes = sorted(
                        changes, key=lambda c: abs(c["new"] - c["old"]), reverse=True
                    )
                    for c in sorted_changes:
                        delta = c["new"] - c["old"]
                        pct = (delta / c["old"] * 100) if c["old"] else 0.0
                        up = delta >= 0
                        color = "green" if up else "red"
                        arrow = "↑" if up else "↓"
                        detail.add_row(
                            c["name"],
                            f"${c['old']:.2f}",
                            f"${c['new']:.2f}",
                            f"[{color}]{delta:+.2f}[/{color}]",
                            f"[{color}]{pct:+.1f}%[/{color}]",
                            f"[{color}]{arrow}[/{color}]",
                        )
                    for d in delisted_items:
                        detail.add_row(
                            d["name"],
                            f"${d['market_low']:.2f}",
                            "—",
                            "—",
                            "—",
                            "[red]WITHDRAWN[/red]",
                        )
                    console.print(detail)

                next_ts = time.time() + interval * 60
                next_str = datetime.fromtimestamp(next_ts).strftime("%I:%M %p")
                console.print(
                    f"\n[dim]Next run at {next_str} ({interval} min). Press Ctrl+C to stop.[/dim]"
                )
                await asyncio.sleep(interval * 60)

        except KeyboardInterrupt:
            console.print("\n[yellow]Watch stopped.[/yellow]")
        finally:
            db.close()
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Watch stopped.[/yellow]")


@cli.command("csv-optimize")
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output CSV path (default: <input>_optimized.csv)",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview changes without writing")
@click.pass_context
def csv_optimize(ctx: click.Context, input_file: Path, output: Path | None, dry_run: bool) -> None:
    """Optimize prices in a TCGPlayer CSV file.

    Reads your exported CSV, computes optimal prices against TCG Low,
    applies safety guardrails, and writes an updated CSV ready for re-upload.
    """

    from scryland.csv_import import process_csv, write_csv

    config: ScrylandConfig = ctx.obj["config"]
    if dry_run:
        config = config.model_copy(update={"dry_run": True})
        console.print("[yellow]DRY RUN — CSV will not be modified[/yellow]")

    rows, report = process_csv(input_file, config)
    _print_report(report)

    if not dry_run and report.updates_applied > 0:
        if output is None:
            output = input_file.with_stem(input_file.stem + "_optimized")
        write_csv(rows, output)
        console.print(f"\n[green]Updated CSV written to: {output}[/green]")
        console.print("Upload this file to TCGPlayer via the seller portal.")
    elif not dry_run and report.updates_applied == 0:
        console.print("\n[green]All prices are already optimal — no file written.[/green]")


@cli.command("add-inventory")
@click.argument("csv_file", type=click.Path(exists=True, path_type=Path))
@click.option("--limit", "-n", type=int, default=None, help="Only process first N cards")
@click.option("--dry-run", is_flag=True, default=False, help="Preview what would be added")
@click.option(
    "--no-save",
    is_flag=True,
    default=False,
    help="Fill in prices but don't click Save — pause for manual verification",
)
@click.option(
    "--skip-lands",
    is_flag=True,
    default=False,
    help="Skip basic land cards (Mountain, Island, Plains, Forest, Swamp, Wastes)",
)
@click.option("--lands-only", is_flag=True, default=False, help="Only process basic land cards")
@click.option(
    "--min-price",
    type=float,
    default=0.25,
    help="Skip cards where TCG Lowest is below this (checked on TCGPlayer). Default: $0.25",
)
@click.option(
    "--csv-min-price",
    type=float,
    default=None,
    help="Pre-filter: skip cards with CSV price below this before searching. "
    "Default: same as --min-price. Use 0 to disable.",
)
@click.option(
    "--price-strategy",
    type=click.Choice(["lowest", "market", "last-sold", "csv"]),
    default="lowest",
    help="Price strategy: lowest (TCG Lowest), market (TCG Market), "
    "last-sold (TCG Last Sold), csv (use CSV price). Default: lowest",
)
@click.option(
    "--defer-manual/--no-defer-manual",
    default=True,
    help="Defer cards needing manual review to the end instead of pausing inline (default: on)",
)
@click.option(
    "--include-sold",
    is_flag=True,
    default=False,
    help="Re-list cards even if the DB shows a prior sale. Use when the CSV "
    "is your full current inventory (i.e. you re-acquired previously-sold cards).",
)
@click.pass_context
def add_inventory(
    ctx: click.Context,
    csv_file: Path,
    limit: int | None,
    dry_run: bool,
    no_save: bool,
    skip_lands: bool,
    lands_only: bool,
    min_price: float,
    csv_min_price: float | None,
    price_strategy: str,
    defer_manual: bool,
    include_sold: bool,
) -> None:
    """Add cards from a Mythic Tools CSV export to TCGPlayer inventory.

    Reads your Mythic Tools export, searches for each card on TCGPlayer,
    and adds it to your inventory with the specified price and quantity.

    After the run, writes <CSV_FILE>_priced.csv with the real TCG-found
    prices substituted into the Price columns. Use that file on subsequent
    runs together with --csv-min-price to skip cards that already hit the
    floor (e.g. $0.01).
    """
    config: ScrylandConfig = ctx.obj["config"]
    logger = ctx.obj["logger"]

    from scryland.mythic_csv import merge_duplicates, read_mythic_csv

    cards = read_mythic_csv(csv_file)
    cards = merge_duplicates(cards)

    if skip_lands:
        basic_lands = {
            "mountain",
            "island",
            "plains",
            "forest",
            "swamp",
            "wastes",
            "snow-covered mountain",
            "snow-covered island",
            "snow-covered plains",
            "snow-covered forest",
            "snow-covered swamp",
        }
        before = len(cards)
        cards = [c for c in cards if c.card_name.lower() not in basic_lands]
        skipped = before - len(cards)
        if skipped:
            console.print(f"Skipped [yellow]{skipped}[/yellow] basic land cards.")

    if lands_only:
        basic_lands = {
            "mountain",
            "island",
            "plains",
            "forest",
            "swamp",
            "wastes",
            "snow-covered mountain",
            "snow-covered island",
            "snow-covered plains",
            "snow-covered forest",
            "snow-covered swamp",
        }
        before = len(cards)
        cards = [c for c in cards if c.card_name.lower() in basic_lands]
        filtered = before - len(cards)
        if filtered:
            console.print(
                f"Filtered to lands only ([yellow]{len(cards)}[/yellow] lands, skipped {filtered} non-lands)."
            )

    # Pre-filter $0.00 cards (tokens etc)
    before = len(cards)
    cards = [c for c in cards if c.effective_price > 0]
    skipped_zero = before - len(cards)
    if skipped_zero:
        console.print(f"Skipped [yellow]{skipped_zero}[/yellow] cards with $0.00 price.")

    # CSV price pre-filter — skip cards too cheap to bother searching on TCGPlayer
    # Default: same as min_price (if CSV price is below min, TCG price will be too)
    if csv_min_price is None:
        csv_floor = min_price
    else:
        csv_floor = csv_min_price

    if csv_floor > 0:
        floor_dec = Decimal(str(csv_floor))
        before = len(cards)
        cards = [c for c in cards if c.effective_price >= floor_dec]
        skipped_csv = before - len(cards)
        if skipped_csv:
            console.print(
                f"Skipped [yellow]{skipped_csv}[/yellow] cards with CSV price "
                f"below ${csv_floor:.2f} (use --csv-min-price 0 to disable)."
            )

    if limit:
        cards = cards[:limit]

    if not cards:
        console.print("[yellow]No cards found to add.[/yellow]")
        return

    console.print(f"Found [cyan]{len(cards)}[/cyan] cards to add.")

    if dry_run:
        from rich.table import Table as RichTable

        t = RichTable(title="Cards to Add (Dry Run)")
        t.add_column("Card Name", style="cyan", max_width=40)
        t.add_column("Set", max_width=30)
        t.add_column("Condition")
        t.add_column("Finish")
        t.add_column("Price", justify="right")
        t.add_column("Qty", justify="right")

        for card in cards:
            t.add_row(
                card.card_name,
                card.set_name,
                card.tcg_condition,
                card.finish,
                f"${card.effective_price:.2f}",
                str(card.quantity),
            )
        console.print(t)
        return

    async def _run() -> None:
        from scryland.browser.flaky import retry_on_flaky
        from scryland.browser.pages.add_inventory import AddInventoryPage
        from scryland.browser.session import BrowserSession
        from scryland.db import InventoryDB, _escape_like

        session = BrowserSession(config)
        db = InventoryDB(Path(config.db_path))

        try:
            db.open()
            await session.start()
            await session.ensure_logged_in()

            add_page = AddInventoryPage(session.page, config)
            added = 0
            skipped = 0
            failed = 0
            results: list[dict] = []  # Track per-card results for summary
            needs_review: list[dict] = []  # Cards deferred for manual review

            for i, card in enumerate(cards):
                console.print(
                    f"\n[cyan]({i + 1}/{len(cards)})[/cyan] "
                    f"{card.card_name} — {card.set_name} "
                    f"({card.tcg_condition}, {card.finish}) "
                    f"${card.effective_price:.2f} x{card.quantity}"
                )

                # Check local DB first — skip if already listed or sold
                finish = "Foil" if card.is_foil else ""
                db_status = db.is_known(card.card_name, card.tcg_condition, finish)
                # When the CSV is the user's full current inventory, an
                # exact "sold" row just means we sold this copy in the past
                # but they own one again — don't let that skip the card.
                if include_sold and db_status == "sold":
                    db_status = None
                if not db_status:
                    # Fuzzy match for active listings
                    if db.is_listed_fuzzy(card.card_name, card.tcg_condition, finish):
                        db_status = "active"
                if not db_status and not include_sold:
                    # Fuzzy match for sold items — if any version of this card was sold, skip it
                    front_face = card.card_name.split("//")[0].strip()
                    sold_match = db.conn.execute(
                        "SELECT product_name FROM inventory "
                        "WHERE (product_name LIKE ? ESCAPE '\\' OR product_name LIKE ? ESCAPE '\\') "
                        "AND status = 'sold'",
                        (f"%{_escape_like(card.card_name)}%", f"%{_escape_like(front_face)}%"),
                    ).fetchone()
                    if sold_match:
                        db_status = "sold"

                if db_status == "active":
                    console.print("  [dim]Already listed — skipping[/dim]")
                    skipped += 1
                    results.append({"card": card, "status": "already listed", "price": None})
                    continue
                elif db_status == "sold":
                    console.print("  [dim]Previously sold — skipping[/dim]")
                    skipped += 1
                    results.append({"card": card, "status": "sold", "price": None})
                    continue

                await session.human_delay()
                await session.dismiss_popups()

                try:
                    # Search for the card
                    await add_page.search_for_card(card)
                    await session.dismiss_popups()

                    # Find and click Add/Manage
                    found, match_score = await add_page.find_and_click_add(card)

                    # Fallback: a strict set-filter search can miss cards
                    # whose Mythic Tools set name doesn't line up exactly
                    # with TCG's dropdown (promos, special editions, etc.).
                    # Retry once with no set filter — find_and_click_add's
                    # collector-# matcher will pick the right product from
                    # the unfiltered results.
                    if not found:
                        logger.info("Retrying '%s' without set filter", card.card_name)
                        await add_page.search_for_card(card, apply_set_filter=False)
                        await session.dismiss_popups()
                        found, match_score = await add_page.find_and_click_add(card)

                    if not found:
                        if defer_manual:
                            csv_price = float(card.effective_price)
                            if csv_price < min_price:
                                console.print(
                                    f"  [yellow]Not found, CSV price ${csv_price:.2f} "
                                    f"below ${min_price:.2f} — skipping[/yellow]"
                                )
                                skipped += 1
                                results.append(
                                    {"card": card, "status": "too cheap", "price": csv_price}
                                )
                                continue
                            console.print("  [yellow]Not found — deferred to end[/yellow]")
                            skipped += 1
                            needs_review.append({"card": card, "price": None})
                            results.append({"card": card, "status": "deferred", "price": None})
                            continue

                        console.print("  [yellow]Not found in search results.[/yellow]")
                        console.print(
                            "  Search is open in the browser. Find and add it manually,"
                            " then press [cyan]Enter[/cyan]. Or [yellow]s[/yellow] to skip."
                        )
                        try:
                            import os

                            os.system("stty sane 2>/dev/null")
                            response = console.input().strip().lower().replace("\r", "")
                        except EOFError:
                            response = "s"

                        if response == "s":
                            skipped += 1
                            results.append({"card": card, "status": "not found", "price": None})
                        else:
                            added += 1
                            results.append(
                                {"card": card, "status": "added (manual)", "price": None}
                            )
                        continue

                    # If not an exact collector number match, handle manually
                    if match_score < 3 and card.collector_number:
                        # Check price first — don't bother with manual review for cheap cards
                        tcg_lowest_check = await add_page.get_tcg_lowest_price(card)
                        csv_price = float(card.effective_price)
                        effective_check = (
                            tcg_lowest_check if tcg_lowest_check is not None else csv_price
                        )
                        if effective_check < min_price:
                            console.print(
                                f"  [yellow]Price ${effective_check:.2f} "
                                f"is below ${min_price:.2f} — skipping[/yellow]"
                            )
                            skipped += 1
                            results.append(
                                {"card": card, "status": "too cheap", "price": effective_check}
                            )
                            back_link = await session.page.query_selector(
                                "a:has-text('Back to Inventory')"
                            )
                            if back_link:
                                await back_link.click()
                            else:
                                await session.page.goto(
                                    config.inventory_url, wait_until="domcontentloaded"
                                )
                            await session.page.wait_for_load_state("domcontentloaded")
                            await session.page.wait_for_timeout(1000)
                            continue

                        if defer_manual:
                            # Defer to end
                            console.print(
                                "  [yellow]Needs manual review — deferred to end[/yellow]"
                            )
                            skipped += 1
                            needs_review.append({"card": card, "price": tcg_lowest_check})
                            results.append(
                                {"card": card, "status": "deferred", "price": tcg_lowest_check}
                            )
                            back_link = await session.page.query_selector(
                                "a:has-text('Back to Inventory')"
                            )
                            if back_link:
                                await back_link.click()
                            else:
                                await session.page.goto(
                                    config.inventory_url, wait_until="domcontentloaded"
                                )
                            await session.page.wait_for_load_state("domcontentloaded")
                            await session.page.wait_for_timeout(1000)
                            continue

                        # Handle inline
                        price_str = (
                            f" (TCG Lowest: ${tcg_lowest_check:.2f})" if tcg_lowest_check else ""
                        )
                        console.print(
                            f"  [yellow]Partial match — collector #{card.collector_number} "
                            f"not confirmed.{price_str}[/yellow]"
                        )
                        console.print(
                            "  [yellow]You're on the page now. Verify this is the right card, "
                            "set price & qty, and Save.[/yellow]"
                        )
                        console.print(
                            "  Press [cyan]Enter[/cyan] when done, or [yellow]s[/yellow] to skip."
                        )
                        try:
                            import os

                            os.system("stty sane 2>/dev/null")
                            response = console.input().strip().lower().replace("\r", "")
                        except EOFError:
                            response = "s"

                        if response == "s":
                            skipped += 1
                            results.append(
                                {"card": card, "status": "skipped (manual)", "price": None}
                            )
                        else:
                            added += 1
                            results.append(
                                {"card": card, "status": "added (manual)", "price": None}
                            )

                        # Go back to catalog
                        back_link = await session.page.query_selector(
                            "a:has-text('Back to Inventory')"
                        )
                        if back_link:
                            await back_link.click()
                        else:
                            await session.page.goto(
                                config.inventory_url, wait_until="domcontentloaded"
                            )
                        await session.page.wait_for_load_state("domcontentloaded")
                        await session.page.wait_for_timeout(1000)
                        continue

                    await session.dismiss_popups()

                    # Check if already listed
                    if await add_page.is_already_listed(card):
                        console.print("  [dim]Already listed — skipping[/dim]")
                        skipped += 1
                        results.append({"card": card, "status": "already listed", "price": None})
                        back_link = await session.page.query_selector(
                            "a:has-text('Back to Inventory')"
                        )
                        if back_link:
                            await back_link.click()
                        else:
                            await session.page.goto(
                                config.inventory_url, wait_until="domcontentloaded"
                            )
                        await session.page.wait_for_load_state("networkidle")
                        continue

                    # Check TCG Lowest price — skip if below min_price
                    tcg_lowest = await add_page.get_tcg_lowest_price(card)
                    if tcg_lowest is not None and tcg_lowest < min_price:
                        console.print(
                            f"  [yellow]TCG Lowest ${tcg_lowest:.2f} "
                            f"is below ${min_price:.2f} — skipping[/yellow]"
                        )
                        skipped += 1
                        results.append({"card": card, "status": "too cheap", "price": tcg_lowest})
                        # Go back to catalog
                        back_link = await session.page.query_selector(
                            "a:has-text('Back to Inventory')"
                        )
                        if back_link:
                            await back_link.click()
                        else:
                            await session.page.goto(
                                config.inventory_url, wait_until="domcontentloaded"
                            )
                        await session.page.wait_for_load_state("networkidle")
                        continue
                    elif tcg_lowest is not None:
                        console.print(f"  TCG Lowest: [green]${tcg_lowest:.2f}[/green]")

                    # Set price and quantity
                    success = await add_page.set_price_and_quantity(
                        card, price_strategy=price_strategy
                    )
                    if not success:
                        console.print("  [yellow]Could not set price — skipping[/yellow]")
                        skipped += 1
                        results.append(
                            {"card": card, "status": "price failed", "price": tcg_lowest}
                        )
                        # Go back to catalog
                        await session.page.goto(config.inventory_url, wait_until="domcontentloaded")
                        await session.page.wait_for_timeout(2000)
                        continue

                    # Save (or pause for verification)
                    if no_save:
                        console.print(
                            "  [yellow]Price and quantity filled — NOT saving (--no-save).[/yellow]"
                        )
                        skipped += 1
                        results.append(
                            {"card": card, "status": "verified (not saved)", "price": tcg_lowest}
                        )
                    else:
                        await session.human_delay()
                        await add_page.save()
                        console.print("  [green]Added![/green]")
                        added += 1
                        results.append({"card": card, "status": "added", "price": tcg_lowest})

                        # Record in local DB to prevent double-posting
                        try:
                            from scryland.models import Listing as ListingModel

                            db_listing = ListingModel(
                                product_name=card.card_name,
                                set_name=card.set_name,
                                condition=card.tcg_condition,
                                quantity=card.quantity,
                                current_price=Decimal(str(tcg_lowest))
                                if tcg_lowest
                                else card.effective_price,
                            )
                            db.upsert_listing(db_listing, finish="Foil" if card.is_foil else "")
                            db.conn.commit()
                        except Exception:
                            logger.debug("Could not record to DB", exc_info=True)

                    # Save() may have triggered a redirect — wait for it to settle,
                    # then navigate directly to catalog (more reliable than clicking
                    # a "Back to Inventory" link that may no longer exist).
                    # The goto races the save's in-flight redirect, which can
                    # raise net::ERR_ABORTED — retry_on_flaky settles and re-tries.
                    try:
                        await session.page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    try:
                        await retry_on_flaky(
                            lambda: session.page.goto(
                                config.inventory_url, wait_until="domcontentloaded"
                            ),
                            page=session.page,
                            label="post-save goto inventory",
                        )
                    except Exception:
                        logger.warning(
                            "Could not return to catalog after save (continuing)",
                            exc_info=True,
                        )
                    try:
                        await session.page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass

                except Exception:
                    logger.warning("Failed to add '%s'", card.card_name, exc_info=True)
                    console.print("  [red]Failed — skipping[/red]")
                    failed += 1
                    results.append({"card": card, "status": "failed", "price": None})
                    # Try to recover by going back to catalog
                    try:
                        await session.page.goto(config.inventory_url, wait_until="domcontentloaded")
                        await session.page.wait_for_timeout(2000)
                    except Exception:
                        pass

            # Print summary
            console.print()
            from rich.table import Table as RichTable

            summary = RichTable(title="Add Inventory Summary")
            summary.add_column("Card", style="cyan", max_width=35)
            summary.add_column("Set", max_width=25)
            summary.add_column("TCG Price", justify="right")
            summary.add_column("Qty", justify="right")
            summary.add_column("Status")

            for r in results:
                card = r["card"]
                price_str = f"${r['price']:.2f}" if r["price"] else "-"
                status = r["status"]
                if status == "added":
                    style = "[green]added[/green]"
                elif status.startswith("verified"):
                    style = "[yellow]verified[/yellow]"
                elif status == "too cheap":
                    style = f"[dim]skipped (${r['price']:.2f} < ${min_price:.2f})[/dim]"
                elif status == "not found":
                    style = "[yellow]not found[/yellow]"
                else:
                    style = f"[red]{status}[/red]"

                summary.add_row(
                    card.card_name,
                    card.set_name,
                    price_str,
                    str(card.quantity),
                    style,
                )

            console.print(summary)
            console.print(
                f"\n[bold]Totals:[/bold] "
                f"[green]{added} added[/green], "
                f"[yellow]{skipped} skipped[/yellow], "
                f"[red]{failed} failed[/red]"
            )

            success_statuses = {"added", "added (manual)", "verified (not saved)"}
            not_added = [
                r
                for r in results
                if r["status"] not in success_statuses and r["status"] != "too cheap"
            ]
            if not_added:
                missed = RichTable(title=f"Not Added ({len(not_added)})")
                missed.add_column("Card", style="cyan", max_width=35)
                missed.add_column("Set", max_width=25)
                missed.add_column("Condition")
                missed.add_column("Finish")
                missed.add_column("Reason", style="yellow")
                for r in not_added:
                    c = r["card"]
                    missed.add_row(
                        c.card_name,
                        c.set_name,
                        c.tcg_condition,
                        c.finish,
                        r["status"],
                    )
                console.print()
                console.print(missed)

            # Rewrite the CSV with the real TCG-found prices so subsequent
            # runs replace the Mythic Tools "guess" price with what we
            # actually saw on TCG. The existing --csv-min-price pre-filter
            # will then automatically skip floor cards on the next run.
            price_overrides: dict[tuple[str, str, str], Decimal] = {}
            for r in results:
                price = r.get("price")
                if price is None:
                    continue
                c = r["card"]
                key = (c.card_name, c.condition, c.finish)
                # If a key already exists, prefer the lower price (floor
                # signal is the one we want to preserve across re-runs).
                existing = price_overrides.get(key)
                new_dec = Decimal(str(price))
                if existing is None or new_dec < existing:
                    price_overrides[key] = new_dec

            if price_overrides:
                from scryland.mythic_csv import write_priced_csv

                # Path.stem strips the LAST suffix, which mangles names like
                # "Mythic Tools List Export (all.ards)" — the ".ards)" gets
                # treated as an extension and the closing paren disappears.
                # Only treat ".csv" (case-insensitive) as a real extension to
                # replace; otherwise just append "_priced.csv" to the full name.
                if csv_file.suffix.lower() == ".csv":
                    priced_path = csv_file.with_name(f"{csv_file.stem}_priced.csv")
                else:
                    priced_path = csv_file.with_name(f"{csv_file.name}_priced.csv")
                try:
                    n_updated = write_priced_csv(csv_file, priced_path, price_overrides)
                    console.print()
                    console.print(
                        f"[bold green]✓ Wrote {n_updated} TCG-found price(s) to:[/bold green]"
                    )
                    console.print(f"  [cyan]{priced_path}[/cyan]")
                    console.print(
                        "[dim]  Re-run with this file to skip floor cards via "
                        "--csv-min-price.[/dim]"
                    )
                except Exception:
                    logger.warning("Could not write priced CSV", exc_info=True)

            # Handle deferred manual review cards
            if needs_review:
                console.print(
                    f"\n[bold yellow]{len(needs_review)} card(s) need manual review:[/bold yellow]"
                )

                review_table = Table(title="Deferred Cards")
                review_table.add_column("Card", style="cyan")
                review_table.add_column("Set")
                review_table.add_column("Collector #", style="yellow")
                review_table.add_column("Qty", justify="right")
                review_table.add_column("TCG Low", justify="right")
                for item in needs_review:
                    c = item["card"]
                    p = f"${item['price']:.2f}" if item["price"] else "-"
                    review_table.add_row(
                        c.card_name, c.set_name, c.collector_number, str(c.quantity), p
                    )
                console.print(review_table)

                console.print("\n[bold]Browser is open — add these manually now.[/bold]")
                for idx, item in enumerate(needs_review):
                    c = item["card"]
                    console.print(
                        f"\n({idx + 1}/{len(needs_review)}) "
                        f"[cyan]{c.card_name}[/cyan] — {c.set_name} "
                        f"(collector #{c.collector_number}) "
                        f"[bold]x{c.quantity}[/bold]"
                    )
                    try:
                        # Search for the card
                        await session.page.goto(config.inventory_url, wait_until="domcontentloaded")
                        await session.page.wait_for_timeout(2000)
                        await session.page.evaluate("""() => {
                            const sel = document.querySelector('#CategoryId');
                            if (!sel) return;
                            for (const opt of sel.options) {
                                if (opt.text.toLowerCase().includes('magic')) {
                                    sel.value = opt.value;
                                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                                    break;
                                }
                            }
                        }""")
                        await session.page.wait_for_timeout(1500)
                        search_input = session.page.locator("#SearchValue")
                        await search_input.fill(c.card_name.split("//")[0].strip())
                        search_btn = session.page.locator(
                            "input[value='Search'], button:has-text('Search')"
                        ).first
                        await search_btn.click()
                        await session.page.wait_for_load_state("networkidle")
                    except Exception:
                        pass

                    console.print(
                        "  Press [cyan]Enter[/cyan] when done, or [yellow]s[/yellow] to skip."
                    )
                    try:
                        import os

                        os.system("stty sane 2>/dev/null")
                        response = console.input().strip().lower().replace("\r", "")
                    except (EOFError, KeyboardInterrupt):
                        break

                console.print("\nClose the browser when finished.")
                try:
                    await session.page.wait_for_event("close", timeout=0)
                except Exception:
                    pass

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception:
            logger.exception("Add inventory failed")
            sys.exit(1)
        finally:
            db.close()
            try:
                await session.close()
            except Exception:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")


def _print_report(report: PricingReport) -> None:
    """Print a pricing report as a rich table."""

    table = Table(title="Pricing Optimization Report")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Total Listings", str(report.total_listings))
    table.add_row("Updates Proposed", str(report.updates_proposed))
    table.add_row("Updates Applied", str(report.updates_applied))
    table.add_row("Updates Skipped", str(report.updates_skipped))
    table.add_row("Updates Rejected", str(report.updates_rejected))
    table.add_row("Updates Failed", str(report.updates_failed))
    table.add_row("Dry Run", "Yes" if report.dry_run else "No")

    console.print(table)

    # Show individual changes
    if report.updates:
        changes_table = Table(title="Price Changes")
        changes_table.add_column("Product", style="cyan", max_width=40)
        changes_table.add_column("Old Price", style="red", justify="right")
        changes_table.add_column("New Price", style="green", justify="right")
        changes_table.add_column("Change %", justify="right")
        changes_table.add_column("Status", style="yellow")

        for update in report.updates:
            pct_str = f"{update.change_pct:+.1f}%"
            changes_table.add_row(
                update.listing.product_name,
                f"${update.old_price:.2f}",
                f"${update.new_price:.2f}",
                pct_str,
                update.status.value,
            )

        console.print(changes_table)


# ---- eBay commands ----


@cli.command("ebay-auth")
@click.pass_context
def ebay_auth(ctx: click.Context) -> None:
    """One-time OAuth flow to authorize Scryland against your eBay account.

    Prerequisites:
      1. At https://developer.ebay.com/my/keys create a keyset and fill in:
         SCRYLAND_EBAY_APP_ID, SCRYLAND_EBAY_CERT_ID, SCRYLAND_EBAY_DEV_ID
      2. Configure a RuName (redirect URL) on the keyset and set
         SCRYLAND_EBAY_REDIRECT_URI_NAME to the RuName (not the URL).
      3. Create business policies (fulfillment/payment/return) in Seller Hub
         and set SCRYLAND_EBAY_FULFILLMENT_POLICY_ID etc.
    """
    config: ScrylandConfig = ctx.obj["config"]

    async def _run() -> None:
        from rich.prompt import Prompt

        from scryland.ebay.auth import EbayAuth

        auth = EbayAuth(config)
        try:
            url = auth.consent_url()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(1)

        # Write the URL to a file so the user can open it without line-wrap
        # issues in their terminal.
        url_file = Path(".scryland_ebay_consent_url.txt")
        url_file.write_text(url + "\n")

        console.print("[bold]Open this URL in your browser and approve:[/bold]\n")
        console.print(f"  [link={url}]{url}[/link]")
        console.print(
            f"\n[dim](Also saved to {url_file} — "
            f"run `xdg-open {url_file}` or `open {url_file}` to open in your "
            f"default app, or `cat {url_file} | xclip -selection clipboard` "
            f"to copy.)[/dim]"
        )
        console.print(
            "\nAfter approving, eBay redirects to your redirect URL. Paste "
            "the ENTIRE redirected URL (or just the [cyan]code=...[/cyan] "
            "value) below."
        )
        raw = Prompt.ask("Redirected URL or code").strip()
        if not raw:
            console.print("[red]No code provided.[/red]")
            sys.exit(1)

        # Accept either the full URL or just the code. If a URL, extract it.
        code = raw
        if "code=" in raw:
            import urllib.parse

            parsed = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [raw])[0]
        # eBay codes are often URL-encoded; decode once more in case.
        import urllib.parse as _up

        code = _up.unquote(code)

        passphrase = Prompt.ask(
            "Choose an encryption passphrase (used again for every listing run)",
            password=True,
        )
        if not passphrase:
            console.print("[red]Passphrase required.[/red]")
            sys.exit(1)
        confirm = Prompt.ask("Confirm passphrase", password=True)
        if confirm != passphrase:
            console.print("[red]Passphrases do not match.[/red]")
            sys.exit(1)

        try:
            await auth.exchange_code(code, passphrase)
        except Exception as exc:
            console.print(f"[red]Token exchange failed: {exc}[/red]")
            sys.exit(1)

        console.print("[green]eBay credentials saved.[/green]")

    asyncio.run(_run())


@cli.command("ebay-bootstrap")
@click.option("--city", required=True)
@click.option("--state", required=True, help="2-letter US state (e.g. TX)")
@click.option("--postal-code", required=True)
@click.option("--country", default="US")
@click.option("--address-line1", default=None)
@click.option("--location-key", default="default")
@click.option(
    "--shipping-cost",
    default=None,
    help="Flat shipping cost the buyer pays (USD). Default: $0.71 "
    "(eBay Standard Envelope), or $4.99 in sandbox.",
)
@click.option(
    "--shipping-service",
    default=None,
    help="eBay shipping service code. Default: US_eBayStandardEnvelope "
    "in production (tracked, cheap, for cards <$20), or "
    "USPSPriority in sandbox. Other options: USPSGroundAdvantage "
    "(any value, ~$5), USPSPriority.",
)
@click.pass_context
def ebay_bootstrap(
    ctx: click.Context,
    city: str,
    state: str,
    postal_code: str,
    country: str,
    address_line1: str | None,
    location_key: str,
    shipping_cost: str,
    shipping_service: str | None,
) -> None:
    """One-shot: create default business policies + inventory location via API.

    Prerequisites (must be done on eBay's website first):
      1. `scryland ebay-auth` completed (we need a user token).
      2. Your account is opted in to Business Policies.

    This creates three policies and an inventory location, then prints the
    env vars you should paste into your .env.
    """
    config: ScrylandConfig = ctx.obj["config"]

    async def _run() -> None:
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.client import EbayClient

        auth = EbayAuth(config)
        passphrase = _ebay_passphrase(config)
        try:
            await auth.access_token(passphrase)
        except Exception as exc:
            console.print(f"[red]eBay auth failed: {exc}[/red]")
            sys.exit(1)

        async with EbayClient(config, auth, passphrase) as ebay:
            console.print(
                f"Creating business policies… (shipping: "
                f"{shipping_service or 'auto'} @ "
                f"{('$' + shipping_cost) if shipping_cost else 'auto'})"
            )
            try:
                policy_ids = await ebay.create_default_policies(
                    shipping_service=shipping_service,
                    shipping_cost=shipping_cost,
                )
            except Exception as exc:
                console.print(f"[red]Policy creation failed: {exc}[/red]")
                sys.exit(1)

            console.print(
                f"  fulfillment: [cyan]{policy_ids['fulfillment']}[/cyan]\n"
                f"  payment:     [cyan]{policy_ids['payment']}[/cyan]\n"
                f"  return:      [cyan]{policy_ids['return']}[/cyan]"
            )

            console.print(f"\nCreating inventory location '{location_key}'…")
            try:
                await ebay.create_merchant_location(
                    location_key,
                    country=country,
                    city=city,
                    state=state,
                    postal_code=postal_code,
                    address_line1=address_line1,
                )
            except Exception as exc:
                console.print(f"[red]Location create failed: {exc}[/red]")
                sys.exit(1)

        console.print("\n[bold green]All set.[/bold green] Add to your .env:\n")
        console.print(
            f"SCRYLAND_EBAY_FULFILLMENT_POLICY_ID={policy_ids['fulfillment']}\n"
            f"SCRYLAND_EBAY_PAYMENT_POLICY_ID={policy_ids['payment']}\n"
            f"SCRYLAND_EBAY_RETURN_POLICY_ID={policy_ids['return']}\n"
            f"SCRYLAND_EBAY_MERCHANT_LOCATION_KEY={location_key}\n"
        )

    asyncio.run(_run())


@cli.command("ebay-policies")
@click.pass_context
def ebay_policies(ctx: click.Context) -> None:
    """List your eBay business-policy IDs (fulfillment, payment, return).

    Copy the IDs into your .env as SCRYLAND_EBAY_*_POLICY_ID.
    """
    config: ScrylandConfig = ctx.obj["config"]

    async def _run() -> None:
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.client import EbayClient

        auth = EbayAuth(config)
        passphrase = _ebay_passphrase(config)
        try:
            await auth.access_token(passphrase)
        except Exception as exc:
            console.print(f"[red]eBay auth failed: {exc}[/red]")
            sys.exit(1)

        async with EbayClient(config, auth, passphrase) as ebay:
            policies = await ebay.list_business_policies()

        for kind in ("fulfillment", "payment", "return"):
            console.print(f"\n[bold]{kind.title()} policies[/bold]")
            items = policies.get(kind, [])
            if not items:
                console.print("  [yellow](none — create one in Seller Hub)[/yellow]")
                continue
            for p in items:
                console.print(f"  [cyan]{p['id']}[/cyan]  {p['name']}")

    asyncio.run(_run())


@cli.command("ebay-setup-location")
@click.option("--key", default="default", help="Location key. Default: 'default'")
@click.option("--country", default="US")
@click.option("--city", required=True)
@click.option("--state", required=True, help="e.g. TX, CA — 2-letter US state")
@click.option("--postal-code", required=True)
@click.option("--address-line1", default=None)
@click.pass_context
def ebay_setup_location(
    ctx: click.Context,
    key: str,
    country: str,
    city: str,
    state: str,
    postal_code: str,
    address_line1: str | None,
) -> None:
    """Create an eBay inventory location (one-time, required before listing).

    Sets SCRYLAND_EBAY_MERCHANT_LOCATION_KEY (use the same `--key` value in
    your .env). If the location already exists, this is a no-op.
    """
    config: ScrylandConfig = ctx.obj["config"]

    async def _run() -> None:
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.client import EbayClient

        auth = EbayAuth(config)
        passphrase = _ebay_passphrase(config)
        try:
            await auth.access_token(passphrase)
        except Exception as exc:
            console.print(f"[red]eBay auth failed: {exc}[/red]")
            sys.exit(1)

        async with EbayClient(config, auth, passphrase) as ebay:
            try:
                await ebay.create_merchant_location(
                    key,
                    country=country,
                    city=city,
                    state=state,
                    postal_code=postal_code,
                    address_line1=address_line1,
                )
            except Exception as exc:
                console.print(f"[red]Location create failed: {exc}[/red]")
                sys.exit(1)

        console.print(
            f"[green]Location '{key}' ready.[/green] "
            f"Set SCRYLAND_EBAY_MERCHANT_LOCATION_KEY={key} in your .env."
        )

    asyncio.run(_run())


@cli.command("ebay-refresh-titles")
@click.option("-n", "--limit", type=int, default=None, help="Only refresh the first N listings.")
@click.option(
    "--dry-run", is_flag=True, default=False, help="Show old → new titles without calling eBay."
)
@click.pass_context
def ebay_refresh_titles(
    ctx: click.Context,
    limit: int | None,
    dry_run: bool,
) -> None:
    """Rebuild titles + aspects for every active eBay listing using the
    current title format.

    Use after changing the title template (or adding rarity/card type
    aspects) to push the new metadata to eBay without relisting.
    """
    config: ScrylandConfig = ctx.obj["config"]
    logger = ctx.obj["logger"]

    async def _run() -> None:
        from dataclasses import dataclass

        from scryland.db import InventoryDB
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.client import EbayClient
        from scryland.ebay.listing import build_listing
        from scryland.ebay.scryfall import ScryfallClient

        @dataclass
        class _CardStub:
            card_name: str
            set_name: str
            collector_number: str
            tcg_condition: str
            quantity: int
            is_foil: bool
            effective_price: float

        db = InventoryDB(Path(config.db_path))
        db.open()
        listings = db.get_ebay_listings(status="active")
        if limit:
            listings = listings[:limit]
        if not listings:
            console.print("[yellow]No active eBay listings to refresh.[/yellow]")
            return

        passphrase = _ebay_passphrase(config)
        auth = EbayAuth(config)
        try:
            await auth.access_token(passphrase)
        except Exception as exc:
            console.print(f"[red]eBay auth failed: {exc}[/red]")
            sys.exit(1)

        updated = 0
        failed = 0
        async with ScryfallClient() as sf, EbayClient(config, auth, passphrase) as ebay:
            for idx, row in enumerate(listings, 1):
                console.print(
                    f"({idx}/{len(listings)}) {row['product_name']} "
                    f"({row['condition']}{' Foil' if row['is_foil'] else ''})"
                )
                info = await sf.find_card(
                    row["product_name"],
                    row["set_name"],
                    row["collector_number"],
                )
                stub = _CardStub(
                    card_name=row["product_name"],
                    set_name=row["set_name"],
                    collector_number=row["collector_number"] or "",
                    tcg_condition=row["condition"] or "Near Mint",
                    quantity=row["quantity"] or 1,
                    is_foil=bool(row["is_foil"]),
                    effective_price=float(row["price"] or 0),
                )
                new_listing = build_listing(stub, info, float(row["price"] or 0))

                # Fetch the current title from eBay for a before/after view.
                old_title = ""
                try:
                    current = await ebay.get_inventory_item(row["sku"])
                    if current:
                        old_title = (current.get("product") or {}).get("title", "")
                except Exception:
                    pass

                if old_title and old_title != new_listing.title:
                    console.print(f"  [dim]old:[/dim] [red]{old_title}[/red]")
                    console.print(f"  [dim]new:[/dim] [green]{new_listing.title}[/green]")
                elif old_title == new_listing.title:
                    console.print(f"  [dim]unchanged:[/dim] {new_listing.title}")
                else:
                    console.print(f"  [dim]new:[/dim] {new_listing.title}")

                if dry_run or old_title == new_listing.title:
                    continue
                try:
                    await ebay._put_inventory_item(new_listing, warnings=[])
                    updated += 1
                    console.print("  [green]✓ updated[/green]")
                except Exception as exc:
                    logger.warning(
                        "refresh inventory item failed for %s: %s",
                        row["product_name"],
                        exc,
                        exc_info=True,
                    )
                    console.print(f"  [red]failed: {exc}[/red]")
                    failed += 1
        console.print(f"\n[bold]Done: {updated} refreshed, {failed} failed.[/bold]")

    asyncio.run(_run())


@cli.command("ebay-update-shipping")
@click.option("--cost", required=True, type=str, help="New shipping cost (USD), e.g. '0.99'")
@click.option("--additional-cost", default="0.00", help="Per-extra-item cost. Default: $0.00")
@click.pass_context
def ebay_update_shipping(
    ctx: click.Context,
    cost: str,
    additional_cost: str,
) -> None:
    """Update the shipping price on the fulfillment policy referenced in .env.

    All existing listings using that policy pick up the new price
    immediately — no per-listing relist needed.
    """
    config: ScrylandConfig = ctx.obj["config"]

    async def _run() -> None:
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.client import EbayClient

        if not config.ebay_fulfillment_policy_id:
            console.print("[red]SCRYLAND_EBAY_FULFILLMENT_POLICY_ID not set.[/red]")
            sys.exit(1)

        auth = EbayAuth(config)
        passphrase = _ebay_passphrase(config)
        try:
            await auth.access_token(passphrase)
        except Exception as exc:
            console.print(f"[red]eBay auth failed: {exc}[/red]")
            sys.exit(1)

        async with EbayClient(config, auth, passphrase) as ebay:
            ok = await ebay.update_fulfillment_shipping_cost(
                config.ebay_fulfillment_policy_id,
                shipping_cost=cost,
                additional_cost=additional_cost,
            )
        if ok:
            console.print(
                f"[green]Fulfillment policy shipping updated to ${cost}.[/green] "
                "All active listings using it reflect the new price."
            )
        else:
            console.print("[red]Update failed — see log for details.[/red]")

    asyncio.run(_run())


@cli.command("ebay-preview")
@click.argument("csv_file", type=click.Path(exists=True, path_type=Path))
@click.option("-n", "--limit", type=int, default=5, help="Preview first N cards. Default: 5")
@click.option(
    "--min-price",
    type=float,
    default=1.00,
    help="Skip cards with CSV price below this. Default: $1.00",
)
@click.option(
    "--show-description",
    is_flag=True,
    default=False,
    help="Print the full HTML description per card.",
)
@click.option(
    "--undercut",
    is_flag=True,
    default=False,
    help="Query eBay for the current lowest matching listing and "
    "show the undercut-by-penny price. Needs SCRYLAND_EBAY_APP_ID "
    "+ SCRYLAND_EBAY_CERT_ID (no user OAuth required).",
)
@click.pass_context
def ebay_preview(
    ctx: click.Context,
    csv_file: Path,
    limit: int,
    min_price: float,
    show_description: bool,
    undercut: bool,
) -> None:
    """Preview eBay listings without hitting the eBay API.

    Looks each card up on Scryfall, builds the full payload, prints title /
    image URL / aspects. Useful while your eBay developer account is pending
    or before doing a `--draft` run.
    """
    config: ScrylandConfig = ctx.obj["config"]

    async def _run() -> None:
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.client import EbayClient
        from scryland.ebay.listing import build_listing
        from scryland.ebay.scryfall import ScryfallClient
        from scryland.mythic_csv import read_mythic_csv

        cards = read_mythic_csv(csv_file)
        cards = [c for c in cards if float(c.effective_price) >= min_price]
        if limit:
            cards = cards[:limit]

        if not cards:
            console.print("[yellow]No cards to preview.[/yellow]")
            return

        console.print(
            f"Previewing [cyan]{len(cards)}[/cyan] card(s) "
            f"(min price ${min_price:.2f})"
            f"{' with undercut lookup' if undercut else ''}.\n"
        )

        ebay: EbayClient | None = None
        auth_obj: EbayAuth | None = None
        if undercut:
            try:
                auth_obj = EbayAuth(config)
                # Pre-flight the app token so we fail fast if keys are missing.
                await auth_obj.app_access_token()
            except Exception as exc:
                console.print(f"[red]Cannot undercut — eBay app token failed: {exc}[/red]")
                sys.exit(1)
            ebay = EbayClient(config, auth_obj, passphrase="")

        try:
            async with ScryfallClient() as sf:
                for idx, card in enumerate(cards, 1):
                    console.print(
                        f"[bold]({idx}/{len(cards)})[/bold] "
                        f"{card.card_name} — {card.set_name} "
                        f"({card.tcg_condition}"
                        f"{' Foil' if card.is_foil else ''}) "
                        f"${card.effective_price:.2f} ×{card.quantity}"
                    )
                    info = await sf.find_card(card.card_name, card.set_name, card.collector_number)
                    if info is None:
                        console.print("  [yellow]Scryfall: no match[/yellow]")
                    else:
                        console.print(
                            f"  [dim]Scryfall: {info.set_code} "
                            f"#{info.collector_number} — {info.rarity}[/dim]"
                        )

                    price = float(card.effective_price)
                    if ebay is not None:
                        try:
                            lowest = await ebay.find_lowest_price(
                                card.card_name,
                                card.set_name,
                                card.collector_number,
                                card.is_foil,
                                condition=card.tcg_condition,
                                include_foil=card.is_foil,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Browse lookup failed for %s: %s",
                                card.card_name,
                                exc,
                            )
                            lowest = None
                        if lowest is None:
                            console.print("  [dim]Undercut: no eBay matches — using CSV[/dim]")
                        else:
                            target = max(round(lowest - 0.01, 2), min_price)
                            console.print(
                                f"  [dim]Undercut: eBay lowest ${lowest:.2f} → "
                                f"would list at ${target:.2f}[/dim]"
                            )
                            price = target

                    listing = build_listing(card, info, price)

                    console.print(f"  SKU:       [cyan]{listing.sku}[/cyan]")
                    console.print(
                        f"  Title:     {listing.title}  [dim]({len(listing.title)}/80)[/dim]"
                    )
                    console.print(
                        f"  Price:     [green]${listing.price_usd:.2f}[/green]  Qty: {listing.quantity}"
                    )
                    console.print(f"  Condition: {listing.condition_id}")
                    console.print(f"  Category:  {listing.category_id}")
                    if listing.image_urls:
                        console.print(f"  Image:     {listing.image_urls[0]}")
                    else:
                        console.print(
                            "  Image:     [yellow](none — eBay conversion will be poor)[/yellow]"
                        )
                    aspects_str = ", ".join(f"{k}={v[0]}" for k, v in listing.aspects.items() if v)
                    console.print(f"  Aspects:   [dim]{aspects_str}[/dim]")
                    if show_description:
                        console.print("  Description:")
                        for line in listing.description_html.splitlines():
                            console.print(f"    [dim]{line}[/dim]")
                    console.print()
        finally:
            if ebay is not None:
                await ebay.close()

    asyncio.run(_run())


@cli.command("list-on-ebay")
@click.argument("csv_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--draft",
    is_flag=True,
    default=False,
    help="Create inventory item + offer but don't publish. Review & publish from eBay Seller Hub.",
)
@click.option("-n", "--limit", type=int, default=None, help="Only process first N cards.")
@click.option(
    "--min-price",
    type=float,
    default=1.00,
    help="Skip cards with CSV price below this. Default: $1.00",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview without calling eBay at all.")
@click.option(
    "--undercut/--no-undercut",
    default=True,
    help="Query eBay for the current lowest matching listing and "
    "price $0.01 below it (floored by --min-price). Default: on.",
)
@click.option(
    "--skip-existing",
    is_flag=True,
    default=False,
    help="Skip cards that already have an active eBay listing in "
    "the DB. Fastest when you only want to list NEW cards.",
)
@click.option(
    "--fast-update/--full-republish",
    default=True,
    help="For cards already listed, only update the price via a "
    "single PUT instead of re-running the whole publish "
    "pipeline (skips Scryfall + inventory PUT). Default: on.",
)
@click.pass_context
def list_on_ebay(
    ctx: click.Context,
    csv_file: Path,
    draft: bool,
    limit: int | None,
    min_price: float,
    dry_run: bool,
    undercut: bool,
    skip_existing: bool,
    fast_update: bool,
) -> None:
    """Create eBay listings for cards from a Mythic Tools CSV.

    Uses Scryfall for card images and oracle text. Requires a one-time
    `scryland ebay-auth` first.
    """
    config: ScrylandConfig = ctx.obj["config"]
    logger = ctx.obj["logger"]

    async def _run() -> None:
        from scryland.db import InventoryDB
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.client import EbayClient
        from scryland.ebay.listing import build_listing
        from scryland.ebay.scryfall import ScryfallClient
        from scryland.mythic_csv import read_mythic_csv

        cards = read_mythic_csv(csv_file)
        # Note: we no longer pre-filter by CSV price. Cards with CSV price
        # below --min-price still go through Browse; if eBay has a live
        # match we use that price instead of skipping.
        if limit:
            cards = cards[:limit]

        if not cards:
            console.print("[yellow]No cards to list.[/yellow]")
            return

        db = InventoryDB(Path(config.db_path))
        db.open()

        console.print(
            f"Will list [cyan]{len(cards)}[/cyan] card(s) on eBay "
            f"[{'DRAFT' if draft else 'LIVE'}] (min ${min_price:.2f}; "
            f"cheap cards get an eBay lookup fallback)."
        )

        if dry_run:
            for c in cards:
                console.print(
                    f"  DRY: {c.card_name} — {c.set_name} "
                    f"({c.tcg_condition}{' Foil' if c.is_foil else ''}) "
                    f"@ ${c.effective_price:.2f} ×{c.quantity}"
                )
            return

        passphrase = _ebay_passphrase(config)

        auth = EbayAuth(config)
        try:
            # Preflight: trigger token load/refresh now so we fail fast if
            # credentials are missing or the passphrase is wrong.
            await auth.access_token(passphrase)
        except Exception as exc:
            console.print(f"[red]eBay auth failed: {exc}[/red]")
            sys.exit(1)

        listed = 0
        failed = 0
        skipped = 0
        # Per-card outcome rows for the end-of-run summary (mirrors
        # add-inventory's results table): {card, status, price, listing_id}.
        results: list[dict] = []

        from scryland.db import canonical_key

        async with ScryfallClient() as sf, EbayClient(config, auth, passphrase) as ebay:
            for idx, card in enumerate(cards, 1):
                console.print(
                    f"\n({idx}/{len(cards)}) {card.card_name} — "
                    f"{card.set_name} ({card.tcg_condition}"
                    f"{' Foil' if card.is_foil else ''}) "
                    f"${card.effective_price:.2f} x{card.quantity}"
                )

                # Look up any existing eBay listing for this exact printing.
                key = canonical_key(
                    card.card_name,
                    card.tcg_condition,
                    card.is_foil,
                    set_name=card.set_name,
                    collector_number=card.collector_number,
                )
                existing = db.find_ebay_listing_by_canonical(key)

                # Fast paths when we already have a listing:
                if existing and existing.get("status") == "active":
                    if skip_existing:
                        console.print("  [dim]Already listed — skipping (--skip-existing)[/dim]")
                        skipped += 1
                        results.append(
                            {
                                "card": card,
                                "status": "already listed",
                                "price": existing.get("price"),
                                "listing_id": existing.get("listing_id"),
                            }
                        )
                        continue
                    if fast_update and not draft and existing.get("offer_id"):
                        ok = await _fast_update_ebay_price(
                            ebay,
                            db,
                            card,
                            existing,
                            undercut,
                            min_price,
                            console,
                        )
                        if ok:
                            listed += 1
                            results.append(
                                {
                                    "card": card,
                                    "status": "price updated",
                                    "price": existing.get("price"),
                                    "listing_id": existing.get("listing_id"),
                                }
                            )
                        else:
                            failed += 1
                            results.append(
                                {
                                    "card": card,
                                    "status": "price update failed",
                                    "price": None,
                                    "listing_id": existing.get("listing_id"),
                                }
                            )
                        continue

                info = await sf.find_card(card.card_name, card.set_name, card.collector_number)
                if info is None:
                    console.print("  [yellow]Scryfall: no match — listing without image[/yellow]")
                elif not info.image_url:
                    console.print("  [yellow]Scryfall: card found but no image[/yellow]")
                else:
                    console.print(
                        f"  [dim]Scryfall: {info.set_code} #{info.collector_number}[/dim]"
                    )

                csv_price = float(card.effective_price)
                price = csv_price
                csv_too_low = csv_price < min_price

                if undercut:
                    try:
                        lowest = await ebay.find_lowest_price(
                            card.card_name,
                            card.set_name,
                            card.collector_number,
                            card.is_foil,
                            condition=card.tcg_condition,
                            include_foil=card.is_foil,
                        )
                    except Exception as exc:
                        logger.warning(
                            "eBay Browse search failed for %s — falling back to CSV price: %s",
                            card.card_name,
                            exc,
                        )
                        lowest = None
                    if lowest is None:
                        if csv_too_low:
                            console.print(
                                f"  [yellow]CSV ${csv_price:.2f} below min "
                                f"${min_price:.2f} and no eBay matches — "
                                f"skipping[/yellow]"
                            )
                            failed += 1
                            results.append(
                                {
                                    "card": card,
                                    "status": "too cheap (no eBay match)",
                                    "price": csv_price,
                                    "listing_id": None,
                                }
                            )
                            continue
                        console.print(
                            f"  [dim]Undercut: no eBay matches found — using CSV ${price:.2f}[/dim]"
                        )
                    else:
                        # Total-price undercut: compare total against total,
                        # then subtract our shipping to get our item price.
                        # Floor honours BOTH eBay's hard $0.99 minimum and
                        # the user's --min-price.
                        our_ship = config.ebay_shipping_cost
                        target_total = lowest - 0.01
                        undercut_target = round(target_total - our_ship, 2)
                        target = round(max(undercut_target, min_price, 0.99), 2)
                        if undercut_target < max(min_price, 0.99):
                            our_total = target + our_ship
                            console.print(
                                f"  [dim]Match: competitor total ${lowest:.2f}; "
                                f"our total will be ${our_total:.2f} at "
                                f"${target:.2f} item + ${our_ship:.2f} ship[/dim]"
                            )
                        else:
                            console.print(
                                f"  [dim]Undercut: competitor total ${lowest:.2f} "
                                f"→ ours ${target + our_ship:.2f} "
                                f"(${target:.2f} + ${our_ship:.2f} ship)"
                                + ("  (CSV was too low — using eBay price)" if csv_too_low else "")
                                + "[/dim]"
                            )
                        price = target
                else:
                    # --no-undercut: if CSV is too low we can't guess, skip.
                    if csv_too_low:
                        console.print(
                            f"  [yellow]CSV ${csv_price:.2f} below min "
                            f"${min_price:.2f} and --no-undercut — skipping[/yellow]"
                        )
                        failed += 1
                        results.append(
                            {
                                "card": card,
                                "status": "too cheap (--no-undercut)",
                                "price": csv_price,
                                "listing_id": None,
                            }
                        )
                        continue

                # Clamp to eBay's hard $0.99 minimum — cheaper than catching
                # a 25016 error at publish time.
                EBAY_HARD_MIN = 0.99
                if price < EBAY_HARD_MIN:
                    console.print(
                        f"  [yellow]Price ${price:.2f} below eBay's $0.99 "
                        f"floor — listing at $0.99[/yellow]"
                    )
                    price = EBAY_HARD_MIN

                listing = build_listing(card, info, price)

                try:
                    result = await ebay.publish_listing(listing, draft=draft)
                except Exception as exc:
                    logger.warning("eBay publish failed for %s", card.card_name, exc_info=True)
                    console.print(f"  [red]Failed: {exc}[/red]")
                    failed += 1
                    # Trim the eBay JSON blob — keep just the human message.
                    err_msg = str(exc)
                    if len(err_msg) > 80:
                        err_msg = err_msg[:77] + "..."
                    results.append(
                        {
                            "card": card,
                            "status": f"publish failed: {err_msg}",
                            "price": price,
                            "listing_id": None,
                        }
                    )
                    continue

                listed += 1
                results.append(
                    {
                        "card": card,
                        "status": "draft" if result.draft else "live",
                        "price": price,
                        "listing_id": result.listing_id,
                    }
                )
                try:
                    db.upsert_ebay_listing(
                        sku=listing.sku,
                        offer_id=result.offer_id,
                        listing_id=result.listing_id,
                        product_name=card.card_name,
                        set_name=card.set_name,
                        collector_number=card.collector_number,
                        condition=card.tcg_condition,
                        is_foil=card.is_foil,
                        price=price,
                        quantity=card.quantity,
                        status="draft" if result.draft else "active",
                    )
                except Exception:
                    logger.warning("Could not persist eBay listing to DB", exc_info=True)

                is_sandbox = config.ebay_environment == "sandbox"
                site = "sandbox.ebay.com" if is_sandbox else "www.ebay.com"
                if result.draft:
                    drafts_url = f"https://{site}/sh/lst/drafts"
                    console.print(f"  [cyan]Draft created (offer {result.offer_id})[/cyan]")
                    console.print(f"  [link={drafts_url}]{drafts_url}[/link]")
                else:
                    item_url = (
                        f"https://{site}/itm/{result.listing_id}"
                        if result.listing_id
                        else f"https://{site}/sh/lst/active"
                    )
                    console.print(f"  [green]Live — listing {result.listing_id}[/green]")
                    console.print(f"  [link={item_url}]{item_url}[/link]")
                for w in result.warnings:
                    console.print(f"    [yellow]warning: {w}[/yellow]")

        # End-of-run summary table mirroring add-inventory's shape.
        if results:
            from rich.table import Table as RichTable

            console.print()
            summary = RichTable(title="List on eBay Summary")
            summary.add_column("Card", style="cyan", max_width=35)
            summary.add_column("Set", max_width=25)
            summary.add_column("Cond")
            summary.add_column("Foil")
            summary.add_column("Price", justify="right")
            summary.add_column("Qty", justify="right")
            summary.add_column("Status")

            success_statuses = {"live", "draft", "price updated"}
            for r in results:
                c = r["card"]
                price = r.get("price")
                price_str = f"${price:.2f}" if price else "-"
                status = r["status"]
                if status == "live":
                    status_cell = "[green]live[/green]"
                elif status == "draft":
                    status_cell = "[cyan]draft[/cyan]"
                elif status == "price updated":
                    status_cell = "[green]price updated[/green]"
                elif status == "already listed":
                    status_cell = "[dim]already listed[/dim]"
                elif status.startswith("too cheap"):
                    status_cell = f"[dim]{status}[/dim]"
                else:
                    status_cell = f"[red]{status}[/red]"
                summary.add_row(
                    c.card_name,
                    c.set_name,
                    c.tcg_condition,
                    "foil" if c.is_foil else "",
                    price_str,
                    str(c.quantity),
                    status_cell,
                )
            console.print(summary)

            # Focused "Not Listed" table — everything that didn't end up
            # live or draft, excluding the intentional "too cheap" drops.
            not_listed = [
                r
                for r in results
                if r["status"] not in success_statuses
                and not r["status"].startswith("too cheap")
                and r["status"] != "already listed"
            ]
            if not_listed:
                missed = RichTable(title=f"Not Listed ({len(not_listed)})")
                missed.add_column("Card", style="cyan", max_width=35)
                missed.add_column("Set", max_width=25)
                missed.add_column("Cond")
                missed.add_column("Foil")
                missed.add_column("Reason", style="yellow")
                for r in not_listed:
                    c = r["card"]
                    missed.add_row(
                        c.card_name,
                        c.set_name,
                        c.tcg_condition,
                        "foil" if c.is_foil else "",
                        r["status"],
                    )
                console.print()
                console.print(missed)

        console.print(
            f"\n[bold]Totals:[/bold] "
            f"[green]{listed} listed[/green], "
            f"[yellow]{skipped} skipped[/yellow], "
            f"[red]{failed} failed[/red]"
        )
        try:
            db.close()
        except Exception:
            pass

    asyncio.run(_run())


# ---- Cross-marketplace commands ----


@cli.command("doctor")
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check Scryland's health — config, DB, eBay, Scryfall connectivity.

    Runs a series of non-destructive checks and reports PASS/FAIL/WARN
    for each. Use this after changing config or credentials to confirm
    everything is wired up.
    """
    config: ScrylandConfig = ctx.obj["config"]

    async def _run() -> None:
        import httpx

        from scryland.db import InventoryDB
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.client import EbayClient

        t = Table(title="Scryland Doctor", show_header=True, header_style="bold")
        t.add_column("Check", style="cyan")
        t.add_column("Status")
        t.add_column("Detail")

        def add(name: str, status: str, detail: str) -> None:
            color = {"PASS": "green", "FAIL": "red", "WARN": "yellow"}.get(status, "")
            t.add_row(name, f"[{color}]{status}[/{color}]", detail)

        # 1. DB open + schema version
        try:
            db = InventoryDB(Path(config.db_path))
            db.open()
            v = db.conn.execute("PRAGMA user_version").fetchone()[0]
            active = db.conn.execute(
                "SELECT COUNT(*) FROM inventory WHERE status='active'"
            ).fetchone()[0]
            ebay_active = db.conn.execute(
                "SELECT COUNT(*) FROM ebay_listings WHERE status='active'"
            ).fetchone()[0]
            db.close()
            add("DB open", "PASS", f"schema v{v}, {active} TCG active, {ebay_active} eBay active")
        except Exception as exc:
            add("DB open", "FAIL", str(exc))

        # 2. eBay credentials
        ebay_configured = bool(config.ebay_app_id and config.ebay_cert_id)
        if not ebay_configured:
            add(
                "eBay credentials",
                "WARN",
                "SCRYLAND_EBAY_APP_ID / CERT_ID not set (skip eBay checks)",
            )
        else:
            add(
                "eBay credentials",
                "PASS",
                f"env={config.ebay_environment}, app id {config.ebay_app_id[:20]}…",
            )

        # 3. eBay auth / user token
        auth = EbayAuth(config) if ebay_configured else None
        user_token_ok = False
        if auth and config.ebay_passphrase:
            try:
                await auth.access_token(config.ebay_passphrase)
                add("eBay user token", "PASS", "refresh token valid")
                user_token_ok = True
            except RuntimeError as exc:
                add("eBay user token", "FAIL", str(exc))
            except Exception as exc:
                add("eBay user token", "FAIL", f"{type(exc).__name__}: {exc}")
        elif auth:
            add("eBay user token", "WARN", "SCRYLAND_EBAY_PASSPHRASE not set — can't test refresh")

        # 4. eBay app token (no user auth needed)
        if auth:
            try:
                await auth.app_access_token()
                add("eBay app token", "PASS", "Browse API ready")
            except Exception as exc:
                add("eBay app token", "FAIL", f"{type(exc).__name__}: {exc}")

        # 5. Business policies configured
        missing = [
            f
            for f in (
                "ebay_fulfillment_policy_id",
                "ebay_payment_policy_id",
                "ebay_return_policy_id",
            )
            if not getattr(config, f)
        ]
        if ebay_configured:
            if missing:
                add("eBay policies", "FAIL", f"missing: {', '.join(missing)} (run ebay-bootstrap)")
            else:
                add("eBay policies", "PASS", "all 3 IDs set")

        # 6. Shipping cost consistency
        if user_token_ok and config.ebay_fulfillment_policy_id:
            try:
                async with EbayClient(config, auth, config.ebay_passphrase) as client:
                    r = await client._http.get(
                        f"/sell/account/v1/fulfillment_policy/{config.ebay_fulfillment_policy_id}",
                        headers=await client._headers(content_json=False),
                    )
                    if r.status_code < 300:
                        body = r.json()
                        # Pull first domestic shipping cost.
                        remote_cost = None
                        for opt in body.get("shippingOptions") or []:
                            if opt.get("optionType") != "DOMESTIC":
                                continue
                            for svc in opt.get("shippingServices") or []:
                                val = (svc.get("shippingCost") or {}).get("value")
                                if val:
                                    remote_cost = float(val)
                                    break
                            if remote_cost is not None:
                                break
                        if remote_cost is None:
                            add(
                                "Shipping policy sync",
                                "WARN",
                                "couldn't extract policy shipping cost",
                            )
                        elif abs(remote_cost - config.ebay_shipping_cost) < 0.005:
                            add(
                                "Shipping policy sync",
                                "PASS",
                                f"config and policy both ${remote_cost:.2f}",
                            )
                        else:
                            add(
                                "Shipping policy sync",
                                "WARN",
                                f"config ${config.ebay_shipping_cost:.2f} "
                                f"vs policy ${remote_cost:.2f} — "
                                f"run ebay-update-shipping to align",
                            )
                    else:
                        add("Shipping policy sync", "WARN", f"policy GET returned {r.status_code}")
            except Exception as exc:
                add("Shipping policy sync", "WARN", f"skipped: {type(exc).__name__}")

        # 7. Seller username resolution
        if user_token_ok:
            try:
                async with EbayClient(config, auth, config.ebay_passphrase) as client:
                    name = await client.get_own_seller_username()
                    if name:
                        add("eBay seller username", "PASS", name)
                    else:
                        add(
                            "eBay seller username",
                            "WARN",
                            "unresolved — set SCRYLAND_EBAY_SELLER_USERNAME",
                        )
            except Exception as exc:
                add("eBay seller username", "WARN", str(exc))

        # 8. Scryfall reachable
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get("https://api.scryfall.com/sets")
                if r.status_code < 300 and r.json().get("data"):
                    add("Scryfall", "PASS", "reachable")
                else:
                    add("Scryfall", "WARN", f"status {r.status_code}")
        except Exception as exc:
            add("Scryfall", "FAIL", f"{type(exc).__name__}: {exc}")

        console.print(t)

    asyncio.run(_run())


@cli.command("compare")
@click.pass_context
def compare(ctx: click.Context) -> None:
    """Side-by-side TCG vs eBay prices, keyed by canonical card identity.

    Shows every card you have listed on either marketplace with: TCG price,
    eBay price, delta, and which side has the listing (BOTH / EBAY ONLY /
    TCG ONLY). No network calls — reads from local DB.
    """
    config: ScrylandConfig = ctx.obj["config"]

    from scryland.db import InventoryDB, _norm_name

    db = InventoryDB(Path(config.db_path))
    db.open()
    try:
        ebay_rows = db.get_ebay_listings(status=None)
        inventory = db.conn.execute("SELECT * FROM inventory WHERE status = 'active'").fetchall()

        def loose_key(product_name: str, condition: str, is_foil: bool) -> str:
            # Matches the non-set portions of canonical_key so TCG rows
            # (which usually lack set/collector) still join to eBay rows.
            # Uses the front face of double-faced cards ("A // B" → "A")
            # since TCG stores only the front face while eBay keeps both.
            front = product_name.split("//")[0]
            return (
                f"{_norm_name(front.split('(')[0])}"
                f"|{condition.replace('Foil', '').strip().lower()}"
                f"|{'F' if is_foil else 'N'}"
            )

        # Map TCG rows by loose key. A single loose key can map to multiple
        # TCG rows if two printings exist under the same name/condition/foil
        # — keep all candidates so the caller can disambiguate by set_name.
        tcg_by_loose: dict[str, list[dict]] = {}
        for r in inventory:
            is_foil = "foil" in (r["finish"] or "").lower()
            k = loose_key(r["product_name"], r["condition"], is_foil)
            tcg_by_loose.setdefault(k, []).append(dict(r))

        def pick_tcg(k: str, ebay_set: str | None) -> tuple[dict | None, bool]:
            """Pick the best TCG candidate for this key.

            Returns (row, ambiguous) where ambiguous=True when multiple
            candidates exist and none matched the eBay set_name (so the
            row we return might be a different printing).
            """
            candidates = tcg_by_loose.get(k)
            if not candidates:
                return None, False
            if ebay_set:
                want = _norm_name(ebay_set)
                for c in candidates:
                    if _norm_name(c.get("set_name") or "") == want:
                        return c, False
            ambiguous = len(candidates) > 1
            return candidates[0], ambiguous

        t = Table(title="TCG vs eBay", show_lines=False, header_style="bold")
        t.add_column("Card", style="cyan", max_width=36)
        t.add_column("Cond")
        t.add_column("Finish")
        t.add_column("TCG $", justify="right")
        t.add_column("eBay $", justify="right")
        t.add_column("Δ", justify="right")
        t.add_column("Δ %", justify="right")
        t.add_column("Status")

        both_count = 0
        only_ebay = 0
        only_tcg = 0
        matched_tcg_keys: set[str] = set()
        # Build rows first so we can sort + color cleanly.
        rows: list[dict] = []
        for e in ebay_rows:
            e_loose = loose_key(
                e["product_name"],
                e["condition"] or "",
                bool(e["is_foil"]),
            )
            tcg, tcg_ambiguous = pick_tcg(e_loose, e.get("set_name"))
            if tcg:
                matched_tcg_keys.add(e_loose)
                if tcg_ambiguous:
                    logger.warning(
                        "compare: multiple TCG candidates for '%s' (%s); "
                        "eBay set '%s' didn't match any — using TCG row '%s' "
                        "from set '%s'. Delta may be wrong-printing.",
                        e["product_name"],
                        e_loose,
                        e.get("set_name"),
                        tcg["product_name"],
                        tcg.get("set_name"),
                    )
            tcg_price = tcg["current_price"] if tcg else None
            ebay_price = e["price"]
            if tcg and e["status"] in ("active", "draft"):
                both_count += 1
                delta = (ebay_price or 0) - (tcg_price or 0)
                status = "both_ambiguous" if tcg_ambiguous else "both"
            elif e["status"] in ("active", "draft"):
                only_ebay += 1
                delta = None
                status = "ebay_only"
            else:
                delta = None
                status = e["status"]
            rows.append(
                {
                    "name": e["product_name"] + (" ?" if tcg_ambiguous else ""),
                    "cond": e["condition"],
                    "finish": "Foil" if e["is_foil"] else "",
                    "tcg": tcg_price,
                    "ebay": ebay_price,
                    "delta": delta,
                    "status": status,
                }
            )
        for k, tcg_candidates in tcg_by_loose.items():
            if k in matched_tcg_keys:
                continue
            # Add every unmatched printing (not just the first) so the
            # user sees all their TCG-only listings, not a deduped view.
            for tcg in tcg_candidates:
                only_tcg += 1
                rows.append(
                    {
                        "name": tcg["product_name"],
                        "cond": tcg["condition"],
                        "finish": tcg["finish"] or "",
                        "tcg": tcg["current_price"],
                        "ebay": None,
                        "delta": None,
                        "status": "tcg_only",
                    }
                )

        # Sort: BOTH first (biggest delta first), then EBAY ONLY, then TCG ONLY.
        order = {"both": 0, "both_ambiguous": 1, "ebay_only": 2, "tcg_only": 3}
        rows.sort(
            key=lambda r: (
                order.get(r["status"], 9),
                -abs(r["delta"]) if r["delta"] is not None else 0,
                r["name"],
            )
        )

        for r in rows:
            status = r["status"]
            delta = r["delta"]
            # Color the Δ column: green when ebay > tcg (we'd earn more on
            # eBay), red when tcg > ebay.
            if delta is None:
                delta_str = "-"
                pct_str = "-"
            else:
                d_color = "green" if delta >= 0 else "red"
                sign = "+" if delta >= 0 else ""
                delta_str = f"[{d_color}]{sign}{delta:.2f}[/{d_color}]"
                pct = (delta / r["tcg"] * 100) if r["tcg"] else 0
                pct_str = f"[{d_color}]{sign}{pct:.0f}%[/{d_color}]"

            if status == "both":
                name_style = "cyan"
                status_cell = "[bold green]BOTH[/bold green]"
            elif status == "both_ambiguous":
                name_style = "cyan"
                status_cell = "[bold orange1]BOTH ?[/bold orange1]"
            elif status == "ebay_only":
                name_style = "magenta"
                status_cell = "[magenta]EBAY ONLY[/magenta]"
            elif status == "tcg_only":
                name_style = "yellow"
                status_cell = "[yellow]TCG ONLY[/yellow]"
            else:
                name_style = "dim"
                status_cell = f"[dim]{status}[/dim]"

            t.add_row(
                f"[{name_style}]{r['name'][:36]}[/{name_style}]",
                r["cond"],
                r["finish"],
                f"${r['tcg']:.2f}" if r["tcg"] else "-",
                f"${r['ebay']:.2f}" if r["ebay"] else "-",
                delta_str,
                pct_str,
                status_cell,
            )

        console.print(t)
        console.print(
            f"\n[cyan]Both:[/cyan] {both_count}   "
            f"[magenta]eBay only:[/magenta] {only_ebay}   "
            f"[yellow]TCG only:[/yellow] {only_tcg}"
        )
    finally:
        db.close()


@cli.command("ebay-sync")
@click.pass_context
def ebay_sync(ctx: click.Context) -> None:
    """Fetch eBay orders via the Sell Fulfillment API and record sales.

    Parallel to `scryland sales` (which uses the TCG browser scrape). When
    an eBay sale is recorded, also auto-ends the matching TCG listing.
    """
    config: ScrylandConfig = ctx.obj["config"]

    async def _run() -> None:
        from scryland.db import InventoryDB
        from scryland.ebay.auth import EbayAuth
        from scryland.ebay.orders import EbayOrdersClient, order_to_sales_rows

        db = InventoryDB(Path(config.db_path))
        db.open()
        try:
            passphrase = _ebay_passphrase(config)
            auth = EbayAuth(config)
            try:
                await auth.access_token(passphrase)
            except Exception as exc:
                console.print(f"[red]eBay auth failed: {exc}[/red]")
                sys.exit(1)

            async with EbayOrdersClient(config, auth, passphrase) as orders_client:
                orders = await orders_client.iter_recent_orders()

            console.print(f"Fetched [cyan]{len(orders)}[/cyan] eBay orders.")

            new_sales = 0
            for order in orders:
                rows = order_to_sales_rows(order)
                new_sales += db.record_order_sales(rows)
                for row in rows:
                    # Cross-delist: end matching TCG listing if active.
                    sku = row.get("_sku")
                    if not sku:
                        continue
                    ebay_listing = db.conn.execute(
                        "SELECT canonical_key FROM ebay_listings WHERE sku = ?",
                        (sku,),
                    ).fetchone()
                    if not ebay_listing:
                        continue
                    key = ebay_listing["canonical_key"]
                    tcg_row = db.find_inventory_by_canonical(key)
                    if tcg_row:
                        console.print(
                            f"  [yellow]TCG listing '{tcg_row['product_name']}' "
                            f"matches sold eBay item — mark for delist in watch/optimize.[/yellow]"
                        )
                    db.mark_ebay_listing_status(sku, "sold")

            if new_sales:
                summary = db.get_sales_summary()
                console.print(f"[green]{new_sales} new sale(s) recorded.[/green]")
                console.print(f"Total net: [green]${summary['total_net']:.2f}[/green]")
            else:
                console.print("No new sales.")
        finally:
            db.close()

    asyncio.run(_run())
