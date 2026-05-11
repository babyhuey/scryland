"""Mythic Tools CSV parser.

Reads a Mythic Tools list export and converts rows into a format
suitable for adding to TCGPlayer inventory.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger("scryland")


@dataclass
class MythicCard:
    """A card from a Mythic Tools CSV export."""

    card_name: str
    set_code: str
    set_name: str
    collector_number: str
    rarity: str
    language: str
    quantity: int
    condition: str  # NM, LP, MP, HP, DMG
    finish: str  # nonfoil, foil, etched
    altered: bool
    signed: bool
    misprint: bool
    price_usd: Decimal
    price_usd_foil: Decimal
    price_usd_etched: Decimal
    scryfall_id: str

    @property
    def effective_price(self) -> Decimal:
        """Return the appropriate price based on finish type."""
        if self.finish == "foil":
            return self.price_usd_foil if self.price_usd_foil > 0 else self.price_usd
        if self.finish == "etched":
            return self.price_usd_etched if self.price_usd_etched > 0 else self.price_usd
        return self.price_usd

    @property
    def tcg_condition(self) -> str:
        """Map Mythic Tools condition to TCGPlayer condition name."""
        mapping = {
            "NM": "Near Mint",
            "LP": "Lightly Played",
            "MP": "Moderately Played",
            "HP": "Heavily Played",
            "DMG": "Damaged",
        }
        return mapping.get(self.condition, "Near Mint")

    @property
    def is_foil(self) -> bool:
        return self.finish in ("foil", "etched")


def read_mythic_csv(path: Path, english_only: bool = True) -> list[MythicCard]:
    """Read a Mythic Tools CSV export.

    Args:
        path: Path to the CSV file.
        english_only: If True, skip non-English cards (default True).

    Returns:
        List of MythicCard objects.
    """
    cards: list[MythicCard] = []

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            language = row.get("Language", "en").strip()
            if english_only and language != "en":
                continue

            try:
                card = MythicCard(
                    card_name=row["Card Name"].strip(),
                    set_code=row.get("Set Code", "").strip(),
                    set_name=row.get("Set Name", "").strip(),
                    collector_number=row.get("Collector Number", "").strip(),
                    rarity=row.get("Rarity", "").strip(),
                    language=language,
                    quantity=int(row.get("Quantity", "1")),
                    condition=row.get("Condition", "NM").strip(),
                    finish=row.get("Finish", "nonfoil").strip(),
                    altered=row.get("Altered", "false").strip().lower() == "true",
                    signed=row.get("Signed", "false").strip().lower() == "true",
                    misprint=row.get("Misprint", "false").strip().lower() == "true",
                    price_usd=Decimal(row.get("Price (USD)", "0") or "0"),
                    price_usd_foil=Decimal(row.get("Price (USD Foil)", "0") or "0"),
                    price_usd_etched=Decimal(row.get("Price (USD Etched)", "0") or "0"),
                    scryfall_id=row.get("Scryfall ID", "").strip(),
                )
                cards.append(card)
            except (ValueError, KeyError) as e:
                logger.warning("Skipping unparseable row: %s", e)

    logger.info("Read %d cards from %s", len(cards), path)
    return cards


def write_priced_csv(
    input_path: Path,
    output_path: Path,
    price_overrides: dict[tuple[str, str, str], Decimal],
) -> int:
    """Rewrite a Mythic Tools CSV with TCG-found prices.

    Reads the original CSV, replaces the price column for each row whose
    (card_name, condition, finish) appears in `price_overrides`, and writes
    the result to `output_path`. The finish drives which price column is
    overwritten — "Price (USD Foil)" / "Price (USD Etched)" / "Price (USD)".

    Returns the number of rows updated.

    Note: the override key is (card_name, condition, finish) only — it
    intentionally does NOT include set/printing because that matches what
    `merge_duplicates` collapses on. If the source CSV contains the same
    name+condition+finish across multiple sets (alt-art reprints, different
    printings), every matching row gets the same TCG-found price written.
    """
    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    updated = 0
    for row in rows:
        key = (
            row.get("Card Name", "").strip(),
            row.get("Condition", "NM").strip(),
            row.get("Finish", "nonfoil").strip(),
        )
        if key not in price_overrides:
            continue
        new_price = price_overrides[key]
        finish = key[2]
        if finish == "foil" and "Price (USD Foil)" in fieldnames:
            row["Price (USD Foil)"] = f"{new_price}"
        elif finish == "etched" and "Price (USD Etched)" in fieldnames:
            row["Price (USD Etched)"] = f"{new_price}"
        elif "Price (USD)" in fieldnames:
            row["Price (USD)"] = f"{new_price}"
        else:
            continue
        updated += 1

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return updated


def merge_duplicates(cards: list[MythicCard]) -> list[MythicCard]:
    """Merge duplicate cards (same name + condition + finish), summing quantities.

    Keeps the first occurrence's metadata (prices, scryfall_id, etc).
    """
    merged: dict[str, MythicCard] = {}
    for card in cards:
        key = f"{card.card_name}|{card.condition}|{card.finish}"
        if key in merged:
            merged[key] = MythicCard(
                **{
                    **vars(merged[key]),
                    "quantity": merged[key].quantity + card.quantity,
                }
            )
        else:
            merged[key] = card

    result = list(merged.values())
    if len(result) < len(cards):
        logger.info(
            "Merged %d duplicate entries → %d unique cards",
            len(cards) - len(result),
            len(result),
        )
    return result
