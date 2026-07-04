"""Shared optimize flow used by `scryland optimize` and `scryland watch`.

Walks the Price Differential Report and for each row either delists (if the
new price would be <= $0.01) or clicks the TCG Lowest Match button and saves.
Only prompts on large price *drops* — increases and penny delists auto-apply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rich.console import Console

from scryland.browser.pages.price_report import PriceReportPage
from scryland.browser.pages.pricing import PricingPage
from scryland.browser.session import BrowserSession
from scryland.config import ScrylandConfig

logger = logging.getLogger("scryland")


@dataclass
class OptimizeResult:
    total: int = 0
    updated: int = 0
    delisted: int = 0
    skipped: int = 0
    failed: int = 0
    total_change: float = 0.0


async def run_price_differential_optimize(
    session: BrowserSession,
    config: ScrylandConfig,
    console: Console,
    db=None,
) -> OptimizeResult:
    """Run one pass of the TCGPlayer Price Differential Report optimize flow.

    Walks every listing that is above the current TCG Lowest, matches it,
    and saves the new price. Skips penny listings (lowest ≤ $0.01) and
    prompts for confirmation on large drops (configurable via config).

    If db is provided, calls db.update_tcg_price after each successful match
    so inventory.current_price stays fresh for the eBay uncompetitive-gap
    delist check. Omit db (e.g. from the standalone optimize command) to
    preserve existing behavior with no DB writes.
    """
    result = OptimizeResult()

    report_page = PriceReportPage(session.page, config)
    await report_page.navigate()
    await session.dismiss_popups()

    differentials = await report_page.get_differentials()
    result.total = len(differentials)

    if not differentials:
        console.print("[green]All prices optimal — nothing to update.[/green]")
        return result

    console.print(f"Found [cyan]{len(differentials)}[/cyan] item(s) needing updates.\n")
    pricing_page = PricingPage(session.page, config)

    for diff in differentials:
        name = diff["product_name"]
        current = diff["marketplace_price"]
        lowest = diff["lowest_listing"]
        change = lowest - current
        # Our own pct — (new - old) / old * 100 → negative = price drop,
        # positive = price increase. Green for up, red for down.
        our_pct = ((lowest - current) / current * 100) if current > 0 else 0.0
        color = "green" if our_pct >= 0 else "red"
        console.print(
            f"  {name} ({diff['condition']}): "
            f"${current:.2f} → ${lowest:.2f} "
            f"[{color}]({our_pct:+.1f}%)[/{color}]"
        )

        # Price floor: a TCG lowest above the $0.01 delist threshold but
        # below the configured floor must not be applied — matching it
        # would undercut our own price floor. Skip rather than clamp to
        # the floor value, since the floor isn't a market price we can
        # actually stand behind.
        if lowest > 0.01 and lowest < config.min_price_floor:
            console.print(
                f"    [yellow]Skipped — ${lowest:.2f} below price floor "
                f"${config.min_price_floor:.2f}[/yellow]"
            )
            result.skipped += 1
            continue

        # Only prompt on big price drops. Skip prompt for increases and penny delists.
        if current > 0:
            change_pct = (lowest - current) / current * 100
        else:
            logger.warning(
                "Row '%s' (%s) has non-positive current price %.2f — skipping change-pct guardrail",
                name,
                diff["condition"],
                current,
            )
            change_pct = 0.0
        from scryland.pricing.guardrails import (
            confirm_with_timeout,
            is_big_price_drop,
        )

        if (
            is_big_price_drop(
                float(current),
                float(lowest),
                config.max_price_change_pct,
                config.max_price_change_abs,
            )
            and lowest > 0.01
        ):
            if not config.dry_run:
                abs_drop = float(current) - float(lowest)
                if not confirm_with_timeout(
                    f"    Change of {change_pct:+.1f}% (-${abs_drop:.2f}) — apply?",
                    default=False,
                    timeout_s=config.prompt_timeout_s,
                ):
                    console.print("    [yellow]Skipped[/yellow]")
                    result.skipped += 1
                    continue

        if config.dry_run:
            console.print("    [yellow]DRY RUN — would update[/yellow]")
            result.skipped += 1
            continue

        if not await report_page.click_manage_for_row(name):
            console.print("    [yellow]Could not find Manage button[/yellow]")
            result.skipped += 1
            continue
        await session.human_delay()
        await session.dismiss_popups()

        # Keep track of which step failed so we can surface *what* went wrong.
        # A `save_changes` failure is especially important: the form already
        # holds the new value, but navigating away discards it unsaved.
        step = "enter_value"
        try:
            if lowest <= 0.01:
                await pricing_page.set_quantity_zero(diff["condition"])
            else:
                await pricing_page.apply_match_lowest(diff["condition"])
            await session.human_delay()
            step = "save"
            await pricing_page.save_changes()
        except Exception as exc:
            if step == "save":
                logger.warning(
                    "save_changes failed for '%s' (%s): %s — form was filled "
                    "but NOT persisted; change discarded on navigation",
                    name,
                    diff["condition"],
                    exc,
                    exc_info=True,
                )
                console.print("    [red]Save FAILED — price entered but not applied[/red]")
            else:
                logger.warning(
                    "Failed to enter new value for '%s' (%s): %s",
                    name,
                    diff["condition"],
                    exc,
                    exc_info=True,
                )
                console.print(f"    [red]Failed ({type(exc).__name__})[/red]")
            result.failed += 1
        else:
            if lowest <= 0.01:
                console.print(f"    [red]Delisted — price would be ${lowest:.2f}[/red]")
                result.delisted += 1
            else:
                console.print("    [green]Updated![/green]")
                result.updated += 1
                result.total_change += change
                if db is not None:
                    db.update_tcg_price(name, diff["condition"], float(lowest))

        await report_page.go_back_to_report()

    return result
