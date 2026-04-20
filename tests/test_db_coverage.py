"""Fill coverage gaps in db.py — price history, sales queries, summary."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scryland.db import InventoryDB
from scryland.models import Listing


@pytest.fixture
def db(tmp_path):
    d = InventoryDB(tmp_path / "t.db")
    d.open()
    yield d
    d.close()


def _listing(name="A", condition="Near Mint", price=1.0, qty=1):
    return Listing(
        product_name=name,
        condition=condition,
        quantity=qty,
        current_price=Decimal(str(price)),
        tcg_low_price=Decimal(str(price)),
    )


class TestPriceHistory:
    def test_record_and_get(self, db):
        db.sync([_listing(price=2.0)])
        history = db.get_price_history("A", "Near Mint")
        assert len(history) >= 1
        assert history[0]["our_price"] == 2.0

    def test_get_price_extremes(self, db):
        db.sync([_listing(price=2.0)])
        db.sync([_listing(price=5.0)])
        db.sync([_listing(price=3.0)])
        extremes = db.get_price_extremes()
        assert len(extremes) >= 1
        row = next(r for r in extremes if r["product_name"] == "A")
        assert row["data_points"] == 3

    def test_record_ebay_price(self, db):
        db.record_ebay_price("Reprieve", "Near Mint", our_price=2.49, competitor_low=2.50)
        history = db.get_price_history("Reprieve")
        assert len(history) == 1
        assert history[0]["marketplace"] == "ebay"
        assert history[0]["our_price"] == 2.49
        assert history[0]["tcg_low"] == 2.50


class TestSalesSummary:
    def _add_sale(self, db, order, product, qty=1, price=1.0, net=0.9, marketplace="tcgplayer"):
        db.conn.execute(
            "INSERT INTO sales "
            "(order_number, order_date, buyer_name, status, product_name, "
            "condition, quantity, sale_price, shipping_amt, total_amount, "
            "fee_amount, net_amount, recorded_at, marketplace) "
            "VALUES (?, '2026-01-01', 'b', 'PAID', ?, 'NM', ?, ?, 0, ?, 0.1, ?, "
            "'2026-01-01', ?)",
            (order, product, qty, price, price * qty, net, marketplace),
        )
        db.conn.commit()

    def test_summary_by_marketplace(self, db):
        self._add_sale(db, "TCG-1", "A", price=2.0, marketplace="tcgplayer")
        self._add_sale(db, "EBAY-1", "B", price=3.0, marketplace="ebay")

        all_s = db.get_sales_summary()
        assert all_s["total_orders"] == 2

        tcg = db.get_sales_summary("tcgplayer")
        assert tcg["total_orders"] == 1
        assert tcg["total_revenue"] == 2.0

        ebay = db.get_sales_summary("ebay")
        assert ebay["total_orders"] == 1
        assert ebay["total_revenue"] == 3.0

    def test_summary_by_marketplace_list(self, db):
        self._add_sale(db, "TCG-1", "A", marketplace="tcgplayer")
        self._add_sale(db, "EBAY-1", "B", marketplace="ebay")
        rows = db.get_sales_summary_by_marketplace()
        names = {r["marketplace"] for r in rows}
        assert names == {"tcgplayer", "ebay"}

    def test_multi_qty_revenue_correct(self, db):
        # Per-unit price $5, qty 3 → revenue $15
        self._add_sale(db, "ORDER", "A", qty=3, price=5.0, net=12.0)
        s = db.get_sales_summary()
        assert s["total_revenue"] == 15.0


class TestCanonicalReclassification:
    def test_recompute_bumps_version(self, db):
        v = db.conn.execute("PRAGMA user_version").fetchone()[0]
        # Migration ran on open → should be at current version.
        assert v == db._CANONICAL_KEY_VERSION

    def test_repeated_open_is_idempotent(self, tmp_path):
        path = tmp_path / "db.db"
        d1 = InventoryDB(path)
        d1.open()
        d1.upsert_ebay_listing(
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
            status="active",
        )
        key1 = d1.conn.execute("SELECT canonical_key FROM ebay_listings WHERE sku='X'").fetchone()[
            0
        ]
        d1.close()

        d2 = InventoryDB(path)
        d2.open()
        key2 = d2.conn.execute("SELECT canonical_key FROM ebay_listings WHERE sku='X'").fetchone()[
            0
        ]
        d2.close()
        assert key1 == key2


class TestReclassifyFalseSold:
    def test_sold_without_sale_record_reset(self, tmp_path):
        """Row marked 'sold' but with no matching sales row should be
        demoted to 'removed' on open (legacy migration)."""
        # Direct sqlite to bypass _migrate running once on open.
        path = tmp_path / "legacy.db"
        InventoryDB(path).open()  # touch
        d = InventoryDB(path)
        d.open()
        d.conn.execute(
            "INSERT INTO inventory (product_name, condition, quantity, "
            "current_price, first_seen, last_seen, status) "
            "VALUES ('Ghost Card', 'Near Mint', 1, 1.0, '2026-01-01', '2026-01-01', 'sold')"
        )
        d.conn.commit()
        d.close()

        # Reopen — reclassification should run.
        d2 = InventoryDB(path)
        d2.open()
        row = d2.conn.execute(
            "SELECT status FROM inventory WHERE product_name='Ghost Card'"
        ).fetchone()
        assert row["status"] == "removed"
        d2.close()
