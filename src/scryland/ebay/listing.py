"""Translate a Mythic CSV row + Scryfall data → eBay Inventory API payload."""

from __future__ import annotations

import re
from dataclasses import dataclass

from scryland.ebay.scryfall import CardInfo
from scryland.mythic_csv import MythicCard

# eBay category id for "Trading Card Games > MTG > MTG Individual Cards"
# is 38292 in the production category tree. Sellers often need to re-verify
# this via /commerce/taxonomy API per marketplace.
DEFAULT_CATEGORY_ID = "183454"  # "CCG Individual Cards" (Magic) — stable id


# eBay changed category 183454 (CCG Individual Cards) in 2023: now only
# `USED_VERY_GOOD` (4000, "Ungraded") or `LIKE_NEW` (2750, "Graded") are
# valid main conditions. The specific grade must be supplied via the
# `conditionDescriptors` field with descriptor id 40001 (Card Condition).
# CCG-specific value IDs differ from Sports TCG — these are the CCG ones.
_UNGRADED_CONDITION_ID = "4000"
_CARD_CONDITION_DESCRIPTOR_ID = "40001"
_CARD_CONDITION_VALUE_ID = {
    "Near Mint": "400010",  # Near Mint or Better
    "Lightly Played": "400015",  # Lightly Played (Excellent)
    "Moderately Played": "400016",  # Moderately Played (Very Good)
    "Heavily Played": "400017",  # Heavily Played (Poor)
    "Damaged": "400017",  # No dedicated ID; map to Heavily Played
}


@dataclass
class EbayListing:
    sku: str
    title: str
    description_html: str
    condition_id: str
    condition_description: str
    image_urls: list[str]
    aspects: dict[str, list[str]]
    condition_descriptors: list[dict]  # {descriptorId, valueIds}
    price_usd: float
    quantity: int
    category_id: str


def build_listing(
    card: MythicCard,
    info: CardInfo | None,
    price_usd: float,
) -> EbayListing:
    """Construct a listing payload from a Mythic CSV row + optional Scryfall card."""
    set_name = info.set_name if info else card.set_name
    collector = info.collector_number if info else card.collector_number
    name = info.name if info else card.card_name

    title = _build_title(card, info, name, set_name, collector)
    sku = _make_sku(name, set_name, collector, card.tcg_condition, card.is_foil)

    description = _build_description(card, info)

    images: list[str] = []
    if info and info.image_url:
        images.append(info.image_url)
    else:
        # eBay listings without images convert very poorly. Surface this
        # loudly so the caller can decide to skip or manually add an image.
        import logging

        logging.getLogger("scryland").warning(
            "Building eBay listing for '%s' with NO image — Scryfall miss. "
            "eBay listing will look like a placeholder in search results.",
            name,
        )

    # All raw MTG singles use the Ungraded main condition; the specific
    # grade is carried in the Card Condition aspect below.
    condition_id = _UNGRADED_CONDITION_ID

    aspects: dict[str, list[str]] = {
        "Game": ["Magic: The Gathering"],
        "Card Name": [name],
        "Set": [set_name],
        "Card Condition": [card.tcg_condition],
        "Finish": ["Foil" if card.is_foil else "Regular"],
        "Card Number": [collector] if collector else [],
        "Language": ["English"],
        "Country/Region of Manufacture": ["United States"],
        # Graded status — these let our listings show up in the common
        # buyer filter "Ungraded only" that most MTG shoppers apply.
        "Graded": ["No"],
        "Professional Grader": ["Not Professionally Graded"],
    }
    if info and info.rarity:
        aspects["Rarity"] = [info.rarity.title()]
    if info and info.type_line:
        aspects["Card Type"] = [info.type_line.split("—")[0].strip()]
    if info and info.colors is not None:
        aspects["Color"] = _colors_from_scryfall(info.colors)

    # Required condition descriptor for trading-cards categories.
    card_cond_value = _CARD_CONDITION_VALUE_ID.get(
        card.tcg_condition,
        _CARD_CONDITION_VALUE_ID["Near Mint"],
    )
    condition_descriptors = [
        {
            "name": _CARD_CONDITION_DESCRIPTOR_ID,
            "values": [card_cond_value],
        }
    ]

    return EbayListing(
        sku=sku,
        title=title,
        description_html=description,
        condition_id=condition_id,
        condition_description=f"{card.tcg_condition} condition. See photos.",
        image_urls=images,
        aspects={k: v for k, v in aspects.items() if v},
        condition_descriptors=condition_descriptors,
        price_usd=price_usd,
        quantity=card.quantity,
        category_id=DEFAULT_CATEGORY_ID,
    )


_COLOR_NAME = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}


def _colors_from_scryfall(letters: list[str]) -> list[str]:
    """Map Scryfall color letters to eBay `Color` aspect values.

    Empty list → 'Colorless'. 2+ colors → 'Multicolor' as a single value
    (eBay's filter treats multi-color cards as one category, not a set
    of individual colors).
    """
    if not letters:
        return ["Colorless"]
    if len(letters) == 1:
        return [_COLOR_NAME.get(letters[0], letters[0])]
    return ["Multicolor"]


_CONDITION_ABBREV = {
    "Near Mint": "NM",
    "Lightly Played": "LP",
    "Moderately Played": "MP",
    "Heavily Played": "HP",
    "Damaged": "DMG",
}


def _build_title(card, info, name: str, set_name: str, collector: str) -> str:
    """Build an eBay listing title.

    Format: ``MTG {name}{ Foil?} {Rarity?} {Set Name} #{Num} {COND}``
    - MTG first: search-keyword anchor.
    - Foil right after the name — foil hunters scan for it.
    - Rarity (from Scryfall) helps buyers evaluate fairness at a glance.
    - Full set name (not the 3-letter code) for readability.
    - Collector number disambiguates reprints.
    - Condition abbreviated (NM/LP/MP/HP/DMG) to save characters.
    Truncated to eBay's 80-char title limit without cutting mid-word.
    """
    cond = _CONDITION_ABBREV.get(card.tcg_condition, card.tcg_condition[:3].upper())
    foil = " Foil" if card.is_foil else ""
    rarity = ""
    if info and info.rarity:
        rarity = " " + info.rarity.title()  # "Rare", "Mythic", etc.

    num_str = f" #{collector}" if collector else ""
    full = f"MTG {name}{foil}{rarity} {set_name}{num_str} {cond}"
    if len(full) <= 80:
        return full

    # Too long — drop pieces in reverse priority order. Each candidate
    # must be strictly shorter than the last so the fallback chain
    # actually makes progress.
    # Priority to keep: MTG, name, condition, foil, number, rarity, set name.
    pieces_trim = [
        # Drop rarity (was first).
        f"MTG {name}{foil} {set_name}{num_str} {cond}",
        # Drop set name (the longest field) to recover lots of chars.
        f"MTG {name}{foil}{num_str} {cond}",
        # Last resort: drop collector number too.
        f"MTG {name}{foil} {cond}",
    ]
    for candidate in pieces_trim:
        if len(candidate) <= 80:
            return candidate
    return pieces_trim[-1][:79].rstrip() + "…"


def _make_sku(name: str, set_name: str, collector: str, condition: str, foil: bool) -> str:
    """Build an eBay SKU ≤50 chars without ever losing the finish marker.

    The finish (F/N) is a load-bearing distinction for eBay — a foil and
    non-foil printing must never collapse to the same SKU. Suffix the
    finish marker LAST (preserving existing SKU layout for already-
    published listings) and reserve space for it before truncation.
    """
    finish = "F" if foil else "N"
    suffix = f"-{finish}"
    budget = 50 - len(suffix)
    body = f"{name}-{set_name}-{collector}-{condition}"
    body = re.sub(r"[^A-Za-z0-9\-]+", "-", body)
    body = body[:budget].strip("-")
    return f"{body}{suffix}"


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _build_description(card: MythicCard, info: CardInfo | None) -> str:
    rows = [
        f"<h2>{info.name if info else card.card_name}</h2>",
        f"<p><strong>Set:</strong> {info.set_name if info else card.set_name}</p>",
        f"<p><strong>Collector #:</strong> {info.collector_number if info else card.collector_number}</p>",
        f"<p><strong>Condition:</strong> {card.tcg_condition}{' (Foil)' if card.is_foil else ''}</p>",
        "<p><strong>Language:</strong> English</p>",
    ]
    if info and info.type_line:
        rows.append(f"<p><strong>Type:</strong> {info.type_line}</p>")
    if info and info.mana_cost:
        rows.append(f"<p><strong>Mana cost:</strong> {info.mana_cost}</p>")
    if info and info.oracle_text:
        rows.append(
            f"<p><strong>Rules text:</strong><br>{info.oracle_text.replace(chr(10), '<br>')}</p>"
        )
    rows.append(
        "<p><em>Card image courtesy of Scryfall. Actual card may vary slightly "
        "due to printing variance. Ships in a sleeve + toploader.</em></p>"
    )
    return "\n".join(rows)
