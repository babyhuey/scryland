"""Price comparison logic — pure functions, no browser dependency."""

from __future__ import annotations

import logging
from decimal import Decimal

from scryland.config import ScrylandConfig
from scryland.models import Listing

logger = logging.getLogger("scryland")


class PriceComparator:
    """Determines the optimal price for a listing based on market data."""

    def __init__(self, config: ScrylandConfig) -> None:
        self._config = config
        self._min_floor = Decimal(str(config.min_price_floor))

    def compute_optimal_price(self, listing: Listing) -> Decimal | None:
        """Determine the optimal price for a listing.

        Strategy: Match the lowest competitive price (tcg_low preferred),
        but never go below the configured price floor.

        Returns:
            The optimal price, or None if no change is needed.
        """
        target = listing.best_comparison_price
        if target is None:
            logger.debug(
                "No comparison price available for '%s', skipping",
                listing.product_name,
            )
            return None

        # Enforce price floor
        target = max(target, self._min_floor)

        # No change needed if already at or below target
        if listing.current_price <= target:
            return None

        return target

    def compute_change_pct(self, old_price: Decimal, new_price: Decimal) -> float:
        """Calculate the percentage change between two prices.

        Returns a negative value for decreases, positive for increases.
        """
        if old_price == 0:
            return 0.0 if new_price == 0 else 100.0
        return float((new_price - old_price) / old_price * 100)
