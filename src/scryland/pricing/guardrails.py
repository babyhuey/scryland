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


_STDIN_CLOSED_NOTICED = False


def confirm_with_timeout(prompt: str, *, default: bool, timeout_s: float) -> bool:
    """Prompt y/N with an auto-default after `timeout_s` seconds.

    Falls back to a synchronous wait when timeout_s <= 0 (legacy behavior),
    so existing one-shot commands keep blocking forever for the user.
    Used by watch with a non-zero timeout so unattended runs progress
    past the "big drop" prompt instead of stalling indefinitely.

    Linux/Unix only — uses select() on stdin. Windows would need a thread.
    On a closed/EOF stdin (nohup, systemd unit without TTY) select returns
    immediately; we detect that case, print a one-time notice, and return
    `default` for every prompt without pretending to wait.
    """
    if timeout_s <= 0:
        return Confirm.ask(prompt, default=default)

    import select
    import sys

    global _STDIN_CLOSED_NOTICED
    if _STDIN_CLOSED_NOTICED:
        return default

    suffix = "Y/n" if default else "y/N"
    print(
        f"{prompt} [{suffix}] (auto-{'yes' if default else 'no'} in {timeout_s:.0f}s): ",
        end="",
        flush=True,
    )
    ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    if not ready:
        print(f" → auto-{'yes' if default else 'no'} (timeout)")
        return default
    response = sys.stdin.readline()
    if response == "":
        # EOF — stdin is closed (nohup, systemd, redirected /dev/null).
        # Latch the notice so we don't spam it every prompt.
        _STDIN_CLOSED_NOTICED = True
        print(f" → stdin closed; auto-defaulting all prompts to {'yes' if default else 'no'}")
        return default
    response = response.strip().lower()
    if response in ("y", "yes"):
        return True
    if response in ("n", "no"):
        return False
    return default


def is_big_price_drop(
    old_price: float,
    new_price: float,
    max_pct: float,
    max_abs: float,
) -> bool:
    """True if a downward price change should be flagged as suspicious.

    Only DROPS count — upward moves are never flagged. Combines a relative
    (percentage) and absolute (dollar) check with AND semantics, so penny
    moves on cheap cards aren't blocked by their large percentage. Set
    max_abs to 0 for pct-only behavior (legacy mode).

    Threshold semantics are inclusive (`>=`): when the user sets
    `--max-price-change-pct 10`, a drop *exactly equal to* 10% should
    block, not slip through. The legacy strict `>` was happenstance.
    """
    if new_price >= old_price or old_price <= 0:
        return False
    pct_drop = (old_price - new_price) / old_price * 100
    abs_drop = old_price - new_price
    if max_abs <= 0:
        return pct_drop >= max_pct
    return pct_drop >= max_pct and abs_drop >= max_abs


class PriceGuardrails:
    """Safety layer that flags and validates price changes before applying."""

    def __init__(self, config: ScrylandConfig) -> None:
        self._config = config
        self._max_change_pct = config.max_price_change_pct
        self._max_change_abs = config.max_price_change_abs
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

        # General-purpose batch path: flag any large change in either
        # direction (data-error guard for csv_import and pricing engine).
        # The CLI optimizer + eBay sweep use `is_big_price_drop` instead,
        # which is drops-only and adds the absolute-dollar floor — that
        # logic is too narrow here, where a big spike could be a data bug.
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
