"""Tests for Mythic Tools CSV parser."""

import csv
from decimal import Decimal

import pytest

from scryland.mythic_csv import MythicCard, read_mythic_csv


@pytest.fixture
def sample_csv(tmp_path):
    """Create a sample Mythic Tools CSV."""
    csv_path = tmp_path / "mythic_export.csv"
    rows = [
        {
            "Card Name": "Lightning Bolt",
            "Set Code": "m21",
            "Set Name": "Core Set 2021",
            "Collector Number": "199",
            "Rarity": "uncommon",
            "Language": "en",
            "Quantity": "4",
            "Condition": "NM",
            "Finish": "nonfoil",
            "Altered": "false",
            "Signed": "false",
            "Misprint": "false",
            "Price (USD)": "1.50",
            "Price (EUR)": "1.30",
            "Price (USD Foil)": "3.00",
            "Price (EUR Foil)": "2.60",
            "Price (USD Etched)": "0",
            "Price (EUR Etched)": "0",
            "Scryfall ID": "abc123",
            "Container Type": "box",
            "Container Name": "test",
        },
        {
            "Card Name": "Rayo",
            "Set Code": "m21",
            "Set Name": "Core Set 2021",
            "Collector Number": "199",
            "Rarity": "uncommon",
            "Language": "es",
            "Quantity": "1",
            "Condition": "NM",
            "Finish": "nonfoil",
            "Altered": "false",
            "Signed": "false",
            "Misprint": "false",
            "Price (USD)": "0.50",
            "Price (EUR)": "0.40",
            "Price (USD Foil)": "0",
            "Price (EUR Foil)": "0",
            "Price (USD Etched)": "0",
            "Price (EUR Etched)": "0",
            "Scryfall ID": "def456",
            "Container Type": "box",
            "Container Name": "test",
        },
        {
            "Card Name": "Sol Ring",
            "Set Code": "c21",
            "Set Name": "Commander 2021",
            "Collector Number": "123",
            "Rarity": "uncommon",
            "Language": "en",
            "Quantity": "1",
            "Condition": "LP",
            "Finish": "foil",
            "Altered": "false",
            "Signed": "false",
            "Misprint": "false",
            "Price (USD)": "2.00",
            "Price (EUR)": "1.80",
            "Price (USD Foil)": "5.00",
            "Price (EUR Foil)": "4.50",
            "Price (USD Etched)": "0",
            "Price (EUR Etched)": "0",
            "Scryfall ID": "ghi789",
            "Container Type": "box",
            "Container Name": "test",
        },
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


class TestReadMythicCsv:
    def test_reads_english_only_by_default(self, sample_csv):
        cards = read_mythic_csv(sample_csv)
        assert len(cards) == 2  # Spanish card filtered out
        assert cards[0].card_name == "Lightning Bolt"
        assert cards[1].card_name == "Sol Ring"

    def test_reads_all_languages(self, sample_csv):
        cards = read_mythic_csv(sample_csv, english_only=False)
        assert len(cards) == 3

    def test_parses_fields_correctly(self, sample_csv):
        cards = read_mythic_csv(sample_csv)
        bolt = cards[0]
        assert bolt.set_name == "Core Set 2021"
        assert bolt.collector_number == "199"
        assert bolt.quantity == 4
        assert bolt.condition == "NM"
        assert bolt.finish == "nonfoil"
        assert bolt.price_usd == Decimal("1.50")
        assert bolt.price_usd_foil == Decimal("3.00")
        assert bolt.scryfall_id == "abc123"

    def test_malformed_price_skips_row_not_whole_import(self, tmp_path):
        """Decimal("$1.00") raises decimal.InvalidOperation, an
        ArithmeticError — not a ValueError/KeyError. One bad row must be
        skipped, not crash the whole read."""
        csv_path = tmp_path / "malformed.csv"
        fieldnames = [
            "Card Name",
            "Set Code",
            "Set Name",
            "Collector Number",
            "Rarity",
            "Language",
            "Quantity",
            "Condition",
            "Finish",
            "Altered",
            "Signed",
            "Misprint",
            "Price (USD)",
            "Price (USD Foil)",
            "Price (USD Etched)",
            "Scryfall ID",
        ]
        rows = [
            {
                "Card Name": "Bad Price Card",
                "Set Code": "m21",
                "Set Name": "Core Set 2021",
                "Collector Number": "1",
                "Rarity": "common",
                "Language": "en",
                "Quantity": "1",
                "Condition": "NM",
                "Finish": "nonfoil",
                "Altered": "false",
                "Signed": "false",
                "Misprint": "false",
                "Price (USD)": "$1.00",
                "Price (USD Foil)": "0",
                "Price (USD Etched)": "0",
                "Scryfall ID": "bad1",
            },
            {
                "Card Name": "Good Card",
                "Set Code": "m21",
                "Set Name": "Core Set 2021",
                "Collector Number": "2",
                "Rarity": "common",
                "Language": "en",
                "Quantity": "1",
                "Condition": "NM",
                "Finish": "nonfoil",
                "Altered": "false",
                "Signed": "false",
                "Misprint": "false",
                "Price (USD)": "1.00",
                "Price (USD Foil)": "0",
                "Price (USD Etched)": "0",
                "Scryfall ID": "good1",
            },
        ]
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        cards = read_mythic_csv(csv_path)
        assert len(cards) == 1
        assert cards[0].card_name == "Good Card"


class TestMythicCard:
    def test_effective_price_nonfoil(self):
        card = MythicCard(
            card_name="Test",
            set_code="",
            set_name="",
            collector_number="",
            rarity="",
            language="en",
            quantity=1,
            condition="NM",
            finish="nonfoil",
            altered=False,
            signed=False,
            misprint=False,
            price_usd=Decimal("1.50"),
            price_usd_foil=Decimal("3.00"),
            price_usd_etched=Decimal("0"),
            scryfall_id="",
        )
        assert card.effective_price == Decimal("1.50")

    def test_effective_price_foil(self):
        card = MythicCard(
            card_name="Test",
            set_code="",
            set_name="",
            collector_number="",
            rarity="",
            language="en",
            quantity=1,
            condition="NM",
            finish="foil",
            altered=False,
            signed=False,
            misprint=False,
            price_usd=Decimal("1.50"),
            price_usd_foil=Decimal("3.00"),
            price_usd_etched=Decimal("0"),
            scryfall_id="",
        )
        assert card.effective_price == Decimal("3.00")

    def test_effective_price_foil_fallback(self):
        card = MythicCard(
            card_name="Test",
            set_code="",
            set_name="",
            collector_number="",
            rarity="",
            language="en",
            quantity=1,
            condition="NM",
            finish="foil",
            altered=False,
            signed=False,
            misprint=False,
            price_usd=Decimal("1.50"),
            price_usd_foil=Decimal("0"),
            price_usd_etched=Decimal("0"),
            scryfall_id="",
        )
        assert card.effective_price == Decimal("1.50")

    def test_tcg_condition_mapping(self):
        for abbr, full in [
            ("NM", "Near Mint"),
            ("LP", "Lightly Played"),
            ("MP", "Moderately Played"),
            ("HP", "Heavily Played"),
            ("DMG", "Damaged"),
        ]:
            card = MythicCard(
                card_name="Test",
                set_code="",
                set_name="",
                collector_number="",
                rarity="",
                language="en",
                quantity=1,
                condition=abbr,
                finish="nonfoil",
                altered=False,
                signed=False,
                misprint=False,
                price_usd=Decimal("1"),
                price_usd_foil=Decimal("0"),
                price_usd_etched=Decimal("0"),
                scryfall_id="",
            )
            assert card.tcg_condition == full

    def test_is_foil(self):
        for finish, expected in [("nonfoil", False), ("foil", True), ("etched", True)]:
            card = MythicCard(
                card_name="Test",
                set_code="",
                set_name="",
                collector_number="",
                rarity="",
                language="en",
                quantity=1,
                condition="NM",
                finish=finish,
                altered=False,
                signed=False,
                misprint=False,
                price_usd=Decimal("1"),
                price_usd_foil=Decimal("2"),
                price_usd_etched=Decimal("3"),
                scryfall_id="",
            )
            assert card.is_foil == expected
