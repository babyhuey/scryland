"""Tests for CSV import/export."""

import csv
from decimal import Decimal

import pytest

from scryland.config import ScrylandConfig
from scryland.csv_import import (
    COL_CONDITION,
    COL_MARKETPLACE_PRICE,
    COL_PRODUCT_NAME,
    COL_QUANTITY,
    COL_SET_NAME,
    COL_TCG_LOW,
    COL_TCG_LOW_SHIPPING,
    COL_TCG_MARKET,
    COL_TCGPLAYER_ID,
    _parse_csv_price,
    csv_row_to_listing,
    process_csv,
    read_csv,
    write_csv,
)


class TestParseCsvPrice:
    def test_normal_price(self):
        assert _parse_csv_price("1.50") == Decimal("1.50")

    def test_with_dollar_sign(self):
        assert _parse_csv_price("$1.50") == Decimal("1.50")

    def test_with_comma(self):
        assert _parse_csv_price("1,234.56") == Decimal("1234.56")

    def test_empty(self):
        assert _parse_csv_price("") is None

    def test_dash(self):
        assert _parse_csv_price("-") is None

    def test_na(self):
        assert _parse_csv_price("N/A") is None

    def test_whitespace(self):
        assert _parse_csv_price("  1.50  ") == Decimal("1.50")


class TestCsvRowToListing:
    def test_basic_row(self):
        row = {
            COL_PRODUCT_NAME: "Lightning Bolt",
            COL_SET_NAME: "Fourth Edition",
            COL_CONDITION: "Near Mint",
            COL_MARKETPLACE_PRICE: "1.50",
            COL_TCG_LOW: "1.20",
            COL_TCG_LOW_SHIPPING: "1.30",
            COL_TCG_MARKET: "1.35",
            COL_QUANTITY: "4",
            COL_TCGPLAYER_ID: "12345",
        }
        listing = csv_row_to_listing(row)
        assert listing is not None
        assert listing.product_name == "Lightning Bolt"
        assert listing.current_price == Decimal("1.50")
        assert listing.tcg_low_price == Decimal("1.20")
        assert listing.quantity == 4
        assert listing.tcgplayer_id == "12345"

    def test_missing_product_name(self):
        row = {COL_PRODUCT_NAME: "", COL_MARKETPLACE_PRICE: "1.50"}
        assert csv_row_to_listing(row) is None

    def test_missing_price(self):
        row = {COL_PRODUCT_NAME: "Test", COL_MARKETPLACE_PRICE: ""}
        assert csv_row_to_listing(row) is None

    def test_missing_quantity_defaults_zero(self):
        row = {
            COL_PRODUCT_NAME: "Test",
            COL_MARKETPLACE_PRICE: "1.50",
        }
        listing = csv_row_to_listing(row)
        assert listing is not None
        assert listing.quantity == 0


class TestProcessCsv:
    @pytest.fixture
    def sample_csv(self, tmp_path):
        """Create a sample CSV file."""
        csv_path = tmp_path / "inventory.csv"
        rows = [
            {
                COL_TCGPLAYER_ID: "1",
                COL_PRODUCT_NAME: "Overpriced Card",
                COL_SET_NAME: "Test Set",
                COL_CONDITION: "Near Mint",
                COL_MARKETPLACE_PRICE: "5.00",
                COL_TCG_LOW: "4.50",
                COL_TCG_LOW_SHIPPING: "4.60",
                COL_TCG_MARKET: "4.75",
                COL_QUANTITY: "1",
            },
            {
                COL_TCGPLAYER_ID: "2",
                COL_PRODUCT_NAME: "Optimal Card",
                COL_SET_NAME: "Test Set",
                COL_CONDITION: "Near Mint",
                COL_MARKETPLACE_PRICE: "3.00",
                COL_TCG_LOW: "3.50",
                COL_TCG_LOW_SHIPPING: "3.60",
                COL_TCG_MARKET: "3.25",
                COL_QUANTITY: "2",
            },
            {
                COL_TCGPLAYER_ID: "3",
                COL_PRODUCT_NAME: "No Stock",
                COL_SET_NAME: "Test Set",
                COL_CONDITION: "Near Mint",
                COL_MARKETPLACE_PRICE: "10.00",
                COL_TCG_LOW: "5.00",
                COL_TCG_LOW_SHIPPING: "5.10",
                COL_TCG_MARKET: "6.00",
                COL_QUANTITY: "0",
            },
        ]
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return csv_path

    def test_dry_run_no_changes(self, sample_csv):
        config = ScrylandConfig(dry_run=True)
        rows, report = process_csv(sample_csv, config)
        assert report.total_listings == 3
        assert report.updates_proposed == 1  # Only "Overpriced Card"
        assert report.updates_applied == 0  # Dry run
        assert report.updates_skipped == 1
        # CSV row should NOT be modified in dry run
        assert rows[0][COL_MARKETPLACE_PRICE] == "5.00"

    def test_applies_changes(self, sample_csv):
        config = ScrylandConfig(dry_run=False)
        rows, report = process_csv(sample_csv, config)
        assert report.updates_applied == 1
        # CSV row should be updated
        assert rows[0][COL_MARKETPLACE_PRICE] == "4.50"
        # Optimal card should not change
        assert rows[1][COL_MARKETPLACE_PRICE] == "3.00"
        # No stock card should not change
        assert rows[2][COL_MARKETPLACE_PRICE] == "10.00"

    def test_skips_zero_quantity(self, sample_csv):
        config = ScrylandConfig(dry_run=False)
        rows, report = process_csv(sample_csv, config)
        # "No Stock" has quantity 0, so even though it's overpriced, skip it
        assert rows[2][COL_MARKETPLACE_PRICE] == "10.00"


class TestWriteCsv:
    def test_round_trip(self, tmp_path):
        rows = [
            {COL_PRODUCT_NAME: "Test", COL_MARKETPLACE_PRICE: "1.50"},
        ]
        output_path = tmp_path / "output.csv"
        write_csv(rows, output_path)

        result = read_csv(output_path)
        assert len(result) == 1
        assert result[0][COL_PRODUCT_NAME] == "Test"
        assert result[0][COL_MARKETPLACE_PRICE] == "1.50"
