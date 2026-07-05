"""Tests for the eBay listing payload builder."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from scryland.ebay.listing import (
    _CARD_CONDITION_VALUE_ID,
    DEFAULT_CATEGORY_ID,
    _build_title,
    _colors_from_scryfall,
    _make_sku,
    _truncate,
    build_listing,
)
from scryland.ebay.scryfall import CardInfo


@dataclass
class _Card:
    """Minimal stand-in for MythicCard — only the attrs build_listing reads."""

    card_name: str
    set_name: str
    collector_number: str
    tcg_condition: str
    quantity: int = 1
    is_foil: bool = False
    effective_price: Decimal = Decimal("1.00")


def _info(
    name="Reprieve",
    set_code="soa",
    set_name="Secrets of Strixhaven: Mystical Archive",
    collector="9",
    rarity="rare",
    colors=None,
    type_line="Instant",
    image_url="https://img/large.png",
):
    return CardInfo(
        name=name,
        set_code=set_code,
        set_name=set_name,
        collector_number=collector,
        image_url=image_url,
        image_small_url="https://img/small.png",
        oracle_text="Exile target spell.",
        type_line=type_line,
        mana_cost="{1}{W}",
        rarity=rarity,
        scryfall_uri="https://scryfall/r",
        colors=colors or ["W"],
    )


class TestColorsMapping:
    def test_mono_white(self):
        assert _colors_from_scryfall(["W"]) == ["White"]

    def test_mono_blue(self):
        assert _colors_from_scryfall(["U"]) == ["Blue"]

    def test_colorless(self):
        assert _colors_from_scryfall([]) == ["Colorless"]

    def test_multicolor_collapses(self):
        assert _colors_from_scryfall(["W", "U"]) == ["Multicolor"]
        assert _colors_from_scryfall(["R", "G", "B"]) == ["Multicolor"]

    def test_unknown_letter_falls_back(self):
        assert _colors_from_scryfall(["Z"]) == ["Z"]


class TestTruncate:
    def test_short_passes(self):
        assert _truncate("abc", 80) == "abc"

    def test_long_gets_ellipsis(self):
        long = "x" * 100
        out = _truncate(long, 80)
        assert len(out) == 80
        assert out.endswith("…")


class TestBuildTitle:
    def test_basic_nonfoil(self):
        card = _Card("Reprieve", "Secrets of Strixhaven: Mystical Archive", "9", "Near Mint")
        t = _build_title(card, _info(), "Reprieve", card.set_name, "9")
        assert "MTG Reprieve" in t
        assert "Rare" in t
        assert "#9" in t
        assert t.endswith(" NM")
        assert "Foil" not in t

    def test_foil_tag(self):
        card = _Card("Hop to It", "Secrets of Strixhaven", "9", "Near Mint", is_foil=True)
        t = _build_title(card, _info(rarity="common"), "Hop to It", card.set_name, "9")
        assert " Foil " in t
        assert t.endswith(" NM")

    def test_condition_abbreviations(self):
        card_lp = _Card("Reprieve", "X", "9", "Lightly Played")
        card_mp = _Card("Reprieve", "X", "9", "Moderately Played")
        assert _build_title(card_lp, None, "Reprieve", "X", "9").endswith(" LP")
        assert _build_title(card_mp, None, "Reprieve", "X", "9").endswith(" MP")

    def test_title_respects_80_char_limit(self):
        long_set = "Secrets of Strixhaven: Mystical Archive Commander Deck Bonus"
        card = _Card("Asmoranomardicadaistinaculdacar // Asmor", long_set, "107", "Near Mint")
        t = _build_title(
            card, _info(set_name=long_set, rarity="mythic"), card.card_name, long_set, "107"
        )
        assert len(t) <= 80

    def test_rarity_omitted_when_info_none(self):
        card = _Card("Reprieve", "X", "9", "Near Mint")
        t = _build_title(card, None, "Reprieve", "X", "9")
        # No rarity
        assert "Rare" not in t and "Common" not in t


class TestMakeSku:
    def test_basic(self):
        sku = _make_sku("Reprieve", "Secrets of Strixhaven", "9", "Near Mint", False)
        assert sku.startswith("Reprieve-Secrets")
        assert sku.endswith("N")  # non-foil

    def test_foil_flag(self):
        sku = _make_sku("Hop to It", "Set", "9", "Near Mint", True)
        assert sku.endswith("F")

    def test_punctuation_stripped(self):
        sku = _make_sku("Forum's Favor", "Secrets!", "9", "Near Mint", False)
        # No apostrophes or !
        assert "'" not in sku
        assert "!" not in sku

    def test_length_cap(self):
        sku = _make_sku("A very long card name " * 5, "Long set", "999", "Near Mint", False)
        assert len(sku) <= 50


class TestConditionDescriptorMap:
    def test_all_known_grades_present(self):
        for grade in (
            "Near Mint",
            "Lightly Played",
            "Moderately Played",
            "Heavily Played",
            "Damaged",
        ):
            assert grade in _CARD_CONDITION_VALUE_ID

    def test_ungraded_near_mint_id(self):
        assert _CARD_CONDITION_VALUE_ID["Near Mint"] == "400010"


class TestBuildListing:
    def test_basic_payload(self):
        card = _Card("Reprieve", "Secrets of Strixhaven: Mystical Archive", "9", "Near Mint")
        listing = build_listing(card, _info(), 2.49)

        assert listing.price_usd == 2.49
        assert listing.quantity == 1
        assert listing.category_id == DEFAULT_CATEGORY_ID
        assert listing.condition_id == "4000"  # ungraded
        assert listing.condition_descriptors == [{"name": "40001", "values": ["400010"]}]
        assert "Magic: The Gathering" in listing.aspects["Game"]
        assert listing.aspects["Graded"] == ["No"]
        assert listing.aspects["Finish"] == ["Regular"]
        assert listing.aspects["Color"] == ["White"]
        assert listing.image_urls == ["https://img/large.png"]

    def test_foil_listing_marks_finish(self):
        card = _Card("Hop to It", "X", "9", "Near Mint", is_foil=True)
        listing = build_listing(card, _info(colors=["G"]), 1.50)
        assert listing.aspects["Finish"] == ["Foil"]
        assert listing.aspects["Color"] == ["Green"]

    def test_no_image_logs_warning(self, caplog):
        import logging

        card = _Card("Unknown", "X", "1", "Near Mint")
        with caplog.at_level(logging.WARNING, logger="scryland"):
            build_listing(card, None, 1.00)
        assert any("NO image" in rec.message for rec in caplog.records)

    def test_double_faced_uses_front_face_in_title(self):
        card = _Card("Honorbound Page // Forum's Favor", "Secrets of Strixhaven", "19", "Near Mint")
        listing = build_listing(
            card,
            _info(
                name="Honorbound Page // Forum's Favor",
                set_name="Secrets of Strixhaven",
                collector="19",
                rarity="common",
            ),
            0.99,
        )
        # Title kept full DFC name but stays under 80c
        assert len(listing.title) <= 80
        assert "Honorbound Page" in listing.title

    def test_missing_collector_omits_card_number_aspect(self):
        card = _Card("X", "Y", "", "Near Mint")
        listing = build_listing(card, None, 1.00)
        assert "Card Number" not in listing.aspects

    def test_unknown_condition_raises_instead_of_defaulting_to_nm(self):
        """An unmapped tcg_condition must not silently list as Near Mint."""
        card = _Card("X", "Y", "9", "Poor")  # not in _CARD_CONDITION_VALUE_ID
        with pytest.raises(ValueError, match="Poor"):
            build_listing(card, None, 1.00)

    def test_csv_set_and_collector_preferred_over_scryfall(self):
        """CSV set/collector must win when present; Scryfall's fuzzy-matched
        printing (a different set/number) must only fill gaps."""
        card = _Card("Reprieve", "CSV Set X", "42", "Near Mint")
        listing = build_listing(card, _info(set_name="Scryfall Set Y", collector="9"), 1.00)
        assert listing.aspects["Set"] == ["CSV Set X"]
        assert listing.aspects["Card Number"] == ["42"]

    def test_scryfall_fills_gaps_when_csv_blank(self):
        card = _Card("Reprieve", "", "", "Near Mint")
        listing = build_listing(card, _info(set_name="Scryfall Set Y", collector="9"), 1.00)
        assert listing.aspects["Set"] == ["Scryfall Set Y"]
        assert listing.aspects["Card Number"] == ["9"]
