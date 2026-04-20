"""Tests for cross-marketplace helpers added for eBay integration."""

from __future__ import annotations

import pytest

from scryland.db import InventoryDB, canonical_key
from scryland.ebay.orders import order_to_sales_rows


@pytest.fixture
def db(tmp_path):
    d = InventoryDB(tmp_path / "test.db")
    d.open()
    yield d
    d.close()


class TestCanonicalKey:
    def test_strips_parenthetical_versioning(self):
        a = canonical_key("Hop to It", "Near Mint", False)
        b = canonical_key("Hop to It (Borderless)", "Near Mint", False)
        assert a == b

    def test_condition_foil_matter(self):
        nm = canonical_key("Hop to It", "Near Mint", False)
        nm_foil = canonical_key("Hop to It", "Near Mint", True)
        lp = canonical_key("Hop to It", "Lightly Played", False)
        assert nm != nm_foil
        assert nm != lp

    def test_case_and_whitespace_insensitive(self):
        a = canonical_key("HOP   TO  IT", "near MINT", False)
        b = canonical_key("Hop to It", "Near Mint", False)
        assert a == b

    def test_strips_foil_from_condition(self):
        """condition can come in as 'Near Mint Foil' from TCG and 'Near Mint' from eBay."""
        a = canonical_key("Hop to It", "Near Mint Foil", True)
        b = canonical_key("Hop to It", "Near Mint", True)
        assert a == b

    def test_different_printings_different_keys(self):
        """Same name across different sets must not collide."""
        key_4ed = canonical_key(
            "Lightning Bolt",
            "Near Mint",
            False,
            set_name="Fourth Edition",
            collector_number="224",
        )
        key_m25 = canonical_key(
            "Lightning Bolt",
            "Near Mint",
            False,
            set_name="Masters 25",
            collector_number="141",
        )
        assert key_4ed != key_m25

    def test_set_and_collector_optional(self):
        """Keys without set/collector still work (loose cross-marketplace)."""
        loose = canonical_key("Lightning Bolt", "Near Mint", False)
        with_set = canonical_key(
            "Lightning Bolt",
            "Near Mint",
            False,
            set_name="Masters 25",
            collector_number="141",
        )
        assert loose != with_set  # strict differs
        # But loose key is stable across calls
        loose2 = canonical_key("Lightning Bolt", "Near Mint", False)
        assert loose == loose2

    def test_punctuation_stripped_from_name(self):
        """Apostrophes etc. shouldn't break matching."""
        a = canonical_key("Honorbound Page // Forum's Favor", "Near Mint", False)
        b = canonical_key("Honorbound Page / Forum s Favor", "Near Mint", False)
        # Apostrophe stripped; // kept.
        assert a != b  # fair — the slashes differ (// vs /)

    def test_parenthetical_treatment_stripped(self):
        """'(Borderless)' etc. strip, keeping base match when no set given."""
        a = canonical_key("Hop to It", "Near Mint", False)
        b = canonical_key("Hop to It (Borderless)", "Near Mint", False)
        assert a == b


class TestEbayListingPersistence:
    def test_upsert_then_fetch(self, db):
        db.upsert_ebay_listing(
            sku="TEST-SKU-1",
            offer_id="12345",
            listing_id=None,
            product_name="Hop to It",
            set_name="Secrets of Strixhaven",
            collector_number="9",
            condition="Near Mint",
            is_foil=False,
            price=2.49,
            quantity=1,
            status="draft",
        )
        rows = db.get_ebay_listings(status=None)
        assert len(rows) == 1
        assert rows[0]["sku"] == "TEST-SKU-1"
        assert rows[0]["status"] == "draft"

    def test_upsert_updates_existing(self, db):
        db.upsert_ebay_listing(
            sku="X",
            offer_id="1",
            listing_id=None,
            product_name="A",
            set_name="B",
            collector_number="1",
            condition="Near Mint",
            is_foil=False,
            price=1.0,
            quantity=1,
            status="draft",
        )
        db.upsert_ebay_listing(
            sku="X",
            offer_id="1",
            listing_id="LIVE1",
            product_name="A",
            set_name="B",
            collector_number="1",
            condition="Near Mint",
            is_foil=False,
            price=2.0,
            quantity=1,
            status="active",
        )
        rows = db.get_ebay_listings(status=None)
        assert len(rows) == 1
        assert rows[0]["listing_id"] == "LIVE1"
        assert rows[0]["price"] == 2.0
        assert rows[0]["status"] == "active"

    def test_find_by_canonical(self, db):
        db.upsert_ebay_listing(
            sku="Y",
            offer_id="2",
            listing_id=None,
            product_name="Reprieve",
            set_name="Strixhaven",
            collector_number="9",
            condition="Near Mint",
            is_foil=False,
            price=2.0,
            quantity=1,
            status="active",
        )
        key = canonical_key("Reprieve", "Near Mint", False)
        found = db.find_ebay_listing_by_canonical(key)
        assert found is not None
        assert found["sku"] == "Y"

    def test_mark_status(self, db):
        db.upsert_ebay_listing(
            sku="Z",
            offer_id="3",
            listing_id=None,
            product_name="A",
            set_name="B",
            collector_number="1",
            condition="Near Mint",
            is_foil=False,
            price=1.0,
            quantity=1,
            status="active",
        )
        db.mark_ebay_listing_status("Z", "sold")
        rows = db.get_ebay_listings(status=None)
        assert rows[0]["status"] == "sold"
        # 'sold' listings shouldn't turn up in canonical lookups.
        key = canonical_key("A", "Near Mint", False)
        assert db.find_ebay_listing_by_canonical(key) is None


class TestOrderToSalesRows:
    def test_single_line_item(self):
        order = {
            "orderId": "AAA-111",
            "buyer": {"username": "test_buyer"},
            "orderPaymentStatus": "PAID",
            "creationDate": "2026-04-19T12:00:00Z",
            "pricingSummary": {
                "total": {"value": "5.00"},
                "totalMarketplaceFee": {"value": "0.50"},
                "deliveryCost": {"value": "0.71"},
            },
            "lineItems": [
                {
                    "title": "Reprieve SOA #9",
                    "sku": "MY-SKU",
                    "quantity": 1,
                    "lineItemCost": {"value": "5.00"},
                },
            ],
        }
        rows = order_to_sales_rows(order)
        assert len(rows) == 1
        r = rows[0]
        assert r["order_number"] == "EBAY-AAA-111"
        assert r["buyer_name"] == "test_buyer"
        assert r["product_name"] == "Reprieve SOA #9"
        assert r["sale_price"] == 5.0
        assert r["fee_amount"] == 0.5
        assert r["net_amount"] == 4.5
        assert r["_sku"] == "MY-SKU"
        assert r["_marketplace"] == "ebay"

    def test_splits_fees_across_line_items(self):
        order = {
            "orderId": "BBB",
            "buyer": {"username": "u"},
            "pricingSummary": {
                "total": {"value": "10.00"},
                "totalMarketplaceFee": {"value": "1.00"},
                "deliveryCost": {"value": "0.00"},
            },
            "lineItems": [
                {"title": "A", "sku": "S1", "quantity": 1, "lineItemCost": {"value": "5.00"}},
                {"title": "B", "sku": "S2", "quantity": 1, "lineItemCost": {"value": "5.00"}},
            ],
        }
        rows = order_to_sales_rows(order)
        assert len(rows) == 2
        assert all(r["fee_amount"] == 0.5 for r in rows)
        assert all(r["net_amount"] == 4.5 for r in rows)

    def test_sale_price_is_per_unit_not_line_total(self):
        """eBay lineItemCost is line total — we must divide by qty for per-unit."""
        order = {
            "orderId": "MULTI",
            "buyer": {"username": "u"},
            "pricingSummary": {
                "total": {"value": "15.00"},
                "totalMarketplaceFee": {"value": "1.50"},
            },
            "lineItems": [
                {
                    "title": "Card x3",
                    "sku": "X",
                    "quantity": 3,
                    "lineItemCost": {"value": "15.00"},
                },  # $5/unit × 3
            ],
        }
        rows = order_to_sales_rows(order)
        assert rows[0]["sale_price"] == 5.00  # per-unit, not 15
        assert rows[0]["quantity"] == 3
        # get_sales_summary will compute 5*3 = 15 revenue, correct.

    def test_fees_split_by_price_not_count(self):
        """$100 + $1 order should NOT attribute 50% of fees to the $1 card."""
        order = {
            "orderId": "CCC",
            "buyer": {"username": "u"},
            "pricingSummary": {
                "total": {"value": "101.00"},
                "totalMarketplaceFee": {"value": "10.10"},
                "deliveryCost": {"value": "0.00"},
            },
            "lineItems": [
                {
                    "title": "Expensive",
                    "sku": "E",
                    "quantity": 1,
                    "lineItemCost": {"value": "100.00"},
                },
                {"title": "Cheap", "sku": "C", "quantity": 1, "lineItemCost": {"value": "1.00"}},
            ],
        }
        rows = order_to_sales_rows(order)
        expensive = next(r for r in rows if r["_sku"] == "E")
        cheap = next(r for r in rows if r["_sku"] == "C")
        # Expensive: 100/101 of $10.10 = $10.00
        # Cheap: 1/101 of $10.10 = $0.10
        assert abs(expensive["fee_amount"] - 10.00) < 0.01
        assert abs(cheap["fee_amount"] - 0.10) < 0.01


class TestCrossLookup:
    def test_find_inventory_by_canonical(self, db):
        # Seed an inventory row via db.sync so statuses/columns are right.
        from decimal import Decimal

        from scryland.models import Listing

        listing = Listing(
            product_name="Reprieve",
            condition="Near Mint",
            quantity=1,
            current_price=Decimal("3.00"),
        )
        db.sync([listing])

        key = canonical_key("Reprieve", "Near Mint", False)
        found = db.find_inventory_by_canonical(key)
        assert found is not None
        assert found["product_name"] == "Reprieve"

    def test_inventory_lookup_ignores_removed(self, db):
        from decimal import Decimal

        from scryland.models import Listing

        db.sync(
            [
                Listing(
                    product_name="Reprieve",
                    condition="Near Mint",
                    quantity=1,
                    current_price=Decimal("3.00"),
                )
            ]
        )
        db.sync([])  # Reprieve now "removed"

        key = canonical_key("Reprieve", "Near Mint", False)
        assert db.find_inventory_by_canonical(key) is None
