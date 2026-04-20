"""Domain models for Scryland."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, computed_field


class UpdateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


class Listing(BaseModel):
    """A single inventory listing on TCGPlayer."""

    product_name: str
    set_name: str = ""
    condition: str = ""
    printing: str = ""  # Normal, Foil, etc.
    quantity: int = 0
    current_price: Decimal
    tcg_low_price: Decimal | None = None
    tcg_low_with_shipping: Decimal | None = None
    tcg_last_sold: Decimal | None = None
    market_price: Decimal | None = None
    tcgplayer_id: str | None = None

    @property
    def best_comparison_price(self) -> Decimal | None:
        """Return the best available comparison price (prefer tcg_low)."""
        if self.tcg_low_price is not None:
            return self.tcg_low_price
        if self.tcg_low_with_shipping is not None:
            return self.tcg_low_with_shipping
        return self.market_price


class PriceUpdate(BaseModel):
    """A proposed price change with safety metadata."""

    listing: Listing
    new_price: Decimal
    old_price: Decimal
    change_pct: float
    status: UpdateStatus = UpdateStatus.PENDING
    requires_confirmation: bool = False

    @computed_field
    @property
    def change_direction(self) -> str:
        if self.new_price < self.old_price:
            return "decrease"
        elif self.new_price > self.old_price:
            return "increase"
        return "none"

    def approve(self) -> None:
        self.status = UpdateStatus.APPROVED

    def reject(self) -> None:
        self.status = UpdateStatus.REJECTED

    def mark_applied(self) -> None:
        self.status = UpdateStatus.APPLIED

    def mark_failed(self) -> None:
        self.status = UpdateStatus.FAILED


class PricingReport(BaseModel):
    """Summary of a pricing optimization run."""

    total_listings: int = 0
    updates_proposed: int = 0
    updates_applied: int = 0
    updates_skipped: int = 0
    updates_rejected: int = 0
    updates_failed: int = 0
    dry_run: bool = False
    timestamp: datetime = datetime.now(UTC)
    updates: list[PriceUpdate] = []


class SyncReport(BaseModel):
    """Summary of an inventory sync operation."""

    timestamp: datetime = datetime.now(UTC)
    total_active: int = 0
    added: list[str] = []
    removed: list[str] = []
    price_changed: list[dict] = []
    quantity_changed: list[dict] = []

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.price_changed or self.quantity_changed)
