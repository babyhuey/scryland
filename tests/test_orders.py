"""Tests for orders/sales tracking."""

import pytest

from scryland.browser.pages.orders import parse_product_name
from scryland.db import InventoryDB


class TestParseProductName:
    def test_standard_format(self):
        name, condition = parse_product_name(
            "Magic - Secret Lair Drop Series: Shredder, Criminal Mastermind "
            "- Higure, the Still Wind - #2368 - Near Mint"
        )
        assert "Shredder" in name or "Higure" in name
        assert condition == "Near Mint"

    def test_foil_condition(self):
        _, condition = parse_product_name("Magic - Some Set: Card Name - #123 - Near Mint Foil")
        assert condition == "Near Mint Foil"

    def test_no_collector_number(self):
        name, condition = parse_product_name("Magic - Core Set 2021: Lightning Bolt - Near Mint")
        assert "Lightning Bolt" in name
        assert condition == "Near Mint"

    def test_lightly_played(self):
        _, condition = parse_product_name("Magic - Set: Card - #1 - Lightly Played")
        assert condition == "Lightly Played"


@pytest.fixture
def db(tmp_path):
    db = InventoryDB(tmp_path / "test.db")
    db.open()
    yield db
    db.close()


class TestRecordSale:
    def test_records_new_sale(self, db):
        sale = {
            "order_number": "ABC-123",
            "order_date": "2026-03-28",
            "buyer_name": "Test Buyer",
            "product_name": "Lightning Bolt",
            "condition": "Near Mint",
            "quantity": 1,
            "sale_price": 1.50,
            "net_amount": 1.20,
        }
        assert db.record_sale(sale) is True

    def test_skips_duplicate(self, db):
        sale = {
            "order_number": "ABC-123",
            "product_name": "Lightning Bolt",
            "condition": "Near Mint",
        }
        assert db.record_sale(sale) is True
        assert db.record_sale(sale) is False

    def test_different_orders_same_card(self, db):
        sale1 = {"order_number": "ABC-123", "product_name": "Bolt", "condition": "NM"}
        sale2 = {"order_number": "DEF-456", "product_name": "Bolt", "condition": "NM"}
        assert db.record_sale(sale1) is True
        assert db.record_sale(sale2) is True


class TestGetKnownOrderNumbers:
    def test_empty(self, db):
        assert db.get_known_order_numbers() == set()

    def test_returns_known(self, db):
        db.record_sale({"order_number": "A", "product_name": "X", "condition": ""})
        db.record_sale({"order_number": "B", "product_name": "Y", "condition": ""})
        assert db.get_known_order_numbers() == {"A", "B"}


class TestSalesSummary:
    def test_empty(self, db):
        s = db.get_sales_summary()
        assert s["total_sales"] == 0
        assert s["total_revenue"] == 0.0

    def test_with_sales(self, db):
        db.record_sale(
            {
                "order_number": "A",
                "product_name": "Card1",
                "condition": "NM",
                "quantity": 2,
                "sale_price": 5.0,
                "fee_amount": 1.0,
                "net_amount": 9.0,
            }
        )
        db.record_sale(
            {
                "order_number": "B",
                "product_name": "Card2",
                "condition": "NM",
                "quantity": 1,
                "sale_price": 3.0,
                "fee_amount": 0.5,
                "net_amount": 2.5,
            }
        )
        s = db.get_sales_summary()
        assert s["total_sales"] == 2
        assert s["total_orders"] == 2
        assert s["total_items_sold"] == 3
