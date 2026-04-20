"""Safety guardrails for price changes."""

from __future__ import annotations

import logging
from decimal import Decimal

from rich.console import Console
from rich.prompt import Confirm

from scryland.config import ScrylandConfig
from scryland.models import PriceUpdate, UpdateStatus

logger = logging.getLogger("scryland")
console = Console()


class PriceGuardrails:
    """Safety layer that flags and validates price changes before applying."""

    def __init__(self, config: ScrylandConfig) -> None:
        self._config = config
        self._max_change_pct = config.max_price_change_pct
        self._min_floor = Decimal(str(config.min_price_floor))

    def check(self, update: PriceUpdate) -> PriceUpdate:
        """Apply safety checks to a proposed price update.

        - Rejects if new price is at or below zero
        - Rejects if new price is below the price floor
        - Flags for confirmation if change exceeds the threshold percentage
        """
        # Reject non-positive prices
        if update.new_price <= 0:
            logger.warning(
                "Rejected price update for '%s': price would be <= 0 ($%.2f)",
                update.listing.product_name,
                update.new_price,
            )
            update.reject()
            return update

        # Reject below floor
        if update.new_price < self._min_floor:
            logger.warning(
                "Rejected price update for '%s': $%.2f is below floor $%.2f",
                update.listing.product_name,
                update.new_price,
                self._min_floor,
            )
            update.reject()
            return update

        # Flag large changes for confirmation
        abs_change = abs(update.change_pct)
        if abs_change > self._max_change_pct:
            update.requires_confirmation = True
            logger.info(
                "Large price change for '%s': %.1f%% (threshold: %.1f%%)",
                update.listing.product_name,
                update.change_pct,
                self._max_change_pct,
            )

        return update

    def prompt_confirmation(self, update: PriceUpdate) -> bool:
        """Interactively ask the user to confirm a large price change.

        Returns True if the user approves.
        """
        console.print()
        console.print("[bold yellow]Large price change detected![/bold yellow]")
        console.print(f"  Product:  [cyan]{update.listing.product_name}[/cyan]")
        if update.listing.set_name:
            console.print(f"  Set:      {update.listing.set_name}")
        if update.listing.condition:
            console.print(f"  Condition:{update.listing.condition}")
        console.print(f"  Old Price:[red] ${update.old_price:.2f}[/red]")
        console.print(f"  New Price:[green] ${update.new_price:.2f}[/green]")
        console.print(f"  Change:   [bold]{update.change_pct:+.1f}%[/bold]")
        console.print()

        return Confirm.ask("Apply this price change?", default=False)

    def validate_batch(self, updates: list[PriceUpdate]) -> list[PriceUpdate]:
        """Apply checks to a batch of updates.

        Also flags if the batch as a whole looks anomalous
        (e.g., all prices dropping significantly).
        """
        checked = [self.check(u) for u in updates]

        # Batch-level sanity check: warn if >50% of updates are large changes
        active = [u for u in checked if u.status == UpdateStatus.PENDING]
        large_changes = [u for u in active if u.requires_confirmation]

        if active and len(large_changes) > len(active) * 0.5:
            console.print(
                f"[bold red]Warning: {len(large_changes)}/{len(active)} updates "
                f"exceed the {self._max_change_pct}% threshold. "
                f"This may indicate a data issue.[/bold red]"
            )

        return checked
