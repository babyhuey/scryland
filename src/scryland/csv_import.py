"""CSV import/export for bulk price management.

Supports reading a TCGPlayer-formatted CSV, applying pricing logic,
and writing an updated CSV for upload — or uploading via browser automation.

TCGPlayer CSV format (expected columns):
    TCGplayer Id, Product Line, Set Name, Product Name, Title, Number,
    Rarity, Condition, TCG Market Price, TCG Direct Low, TCG Low Price,
    TCG Low Price With Shipping, TCG Marketplace Price, Add to Quantity,
    Photo URL

The key columns for pricing:
    - TCG Marketplace Price: your current listed price (editable)
    - TCG Low Price: the lowest competitor price
    - TCG Low Price With Shipping: lowest price including shipping
    - TCG Market Price: the market average
    - Add to Quantity: how many you're listing
"""

from __future__ import annotations

import csv
import logging
from decimal import Decimal
from pathlib import Path

from scryland.config import ScrylandConfig
from scryland.models import Listing, PriceUpdate, PricingReport
from scryland.pricing.comparator import PriceComparator
from scryland.pricing.guardrails import PriceGuardrails

logger = logging.getLogger("scryland")

# Expected column names (TCGPlayer CSV format)
COL_TCGPLAYER_ID = "TCGplayer Id"
COL_PRODUCT_LINE = "Product Line"
COL_SET_NAME = "Set Name"
COL_PRODUCT_NAME = "Product Name"
COL_TITLE = "Title"
COL_NUMBER = "Number"
COL_RARITY = "Rarity"
COL_CONDITION = "Condition"
COL_TCG_MARKET = "TCG Market Price"
COL_TCG_DIRECT_LOW = "TCG Direct Low"
COL_TCG_LOW = "TCG Low Price"
COL_TCG_LOW_SHIPPING = "TCG Low Price With Shipping"
COL_MARKETPLACE_PRICE = "TCG Marketplace Price"
COL_QUANTITY = "Add to Quantity"
COL_PHOTO_URL = "Photo URL"


def _parse_csv_price(value: str) -> Decimal | None:
    """Parse a price value from the CSV."""
    if not value or value.strip() in ("", "-", "N/A"):
        return None
    cleaned = value.strip().replace("$", "").replace(",", "")
    try:
        return Decimal(cleaned)
    except Exception:
        logger.warning("Could not parse CSV price: '%s'", value)
        return None


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read a TCGPlayer CSV file and return rows as dicts."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    logger.info("Read %d rows from %s", len(rows), path)
    return rows


def csv_row_to_listing(row: dict[str, str]) -> Listing | None:
    """Convert a CSV row dict to a Listing model."""
    product_name = row.get(COL_PRODUCT_NAME, "").strip()
    if not product_name:
        return None

    current_price = _parse_csv_price(row.get(COL_MARKETPLACE_PRICE, ""))
    if current_price is None:
        return None

    quantity = 0
    qty_str = row.get(COL_QUANTITY, "0").strip()
    try:
        quantity = int(qty_str) if qty_str else 0
    except ValueError:
        pass

    return Listing(
        product_name=product_name,
        set_name=row.get(COL_SET_NAME, "").strip(),
        condition=row.get(COL_CONDITION, "").strip(),
        printing=row.get(COL_RARITY, "").strip(),
        quantity=quantity,
        current_price=current_price,
        tcg_low_price=_parse_csv_price(row.get(COL_TCG_LOW, "")),
        tcg_low_with_shipping=_parse_csv_price(row.get(COL_TCG_LOW_SHIPPING, "")),
        market_price=_parse_csv_price(row.get(COL_TCG_MARKET, "")),
        tcgplayer_id=row.get(COL_TCGPLAYER_ID, "").strip() or None,
    )


def process_csv(
    input_path: Path,
    config: ScrylandConfig,
) -> tuple[list[dict[str, str]], PricingReport]:
    """Process a TCGPlayer CSV: read, compute optimal prices, apply guardrails.

    Returns the modified rows (with updated prices) and a report.
    Does NOT write the file — call write_csv() separately.
    """
    comparator = PriceComparator(config)
    guardrails = PriceGuardrails(config)

    rows = read_csv(input_path)
    report = PricingReport(dry_run=config.dry_run)
    report.total_listings = len(rows)

    updates: list[tuple[int, PriceUpdate]] = []  # (row_index, update)

    for i, row in enumerate(rows):
        listing = csv_row_to_listing(row)
        if not listing or listing.quantity <= 0:
            continue

        optimal = comparator.compute_optimal_price(listing)
        if optimal is None:
            continue

        change_pct = comparator.compute_change_pct(listing.current_price, optimal)
        update = PriceUpdate(
            listing=listing,
            new_price=optimal,
            old_price=listing.current_price,
            change_pct=change_pct,
        )
        updates.append((i, update))

    if not updates:
        logger.info("All CSV prices are already optimal")
        return rows, report

    # Apply guardrails
    all_updates = [u for _, u in updates]
    all_updates = guardrails.validate_batch(all_updates)
    report.updates_proposed = len(all_updates)

    # Handle confirmations and apply to CSV rows
    for (row_idx, _), update in zip(updates, all_updates, strict=True):
        if update.status.value == "rejected":
            report.updates_rejected += 1
            report.updates.append(update)
            continue

        if update.requires_confirmation:
            if guardrails.prompt_confirmation(update):
                update.approve()
            else:
                update.reject()
                report.updates_rejected += 1
                report.updates.append(update)
                continue
        else:
            update.approve()

        if config.dry_run:
            logger.info(
                "DRY RUN: Would update '%s' (%s) from $%.2f to $%.2f",
                update.listing.product_name,
                update.listing.condition,
                update.old_price,
                update.new_price,
            )
            report.updates_skipped += 1
        else:
            # Update the price in the CSV row
            rows[row_idx][COL_MARKETPLACE_PRICE] = f"{update.new_price:.2f}"
            update.mark_applied()
            report.updates_applied += 1

        report.updates.append(update)

    return rows, report


def write_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    """Write processed rows back to a CSV file."""
    if not rows:
        logger.warning("No rows to write")
        return

    fieldnames = list(rows[0].keys())
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Wrote %d rows to %s", len(rows), output_path)
