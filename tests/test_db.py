"""Tests for inventory database."""

from decimal import Decimal

import pytest

from scryland.db import InventoryDB
from scryland.models import Listing


@pytest.fixture
def db(tmp_path):
    """Create a temporary database."""
    db = InventoryDB(tmp_path / "test.db")
    db.open()
    yield db
    db.close()


def _make_listing(**kwargs) -> Listing:
    defaults = {
        "product_name": "Lightning Bolt",
        "set_name": "Fourth Edition",
        "condition": "Near Mint",
        "quantity": 1,
        "current_price": Decimal("1.50"),
        "tcg_low_price": Decimal("1.20"),
        "market_price": Decimal("1.35"),
    }
    defaults.update(kwargs)
    return Listing(**defaults)


class TestUpsertListing:
    def test_insert_new(self, db):
        listing = _make_listing()
        is_new = db.upsert_listing(listing)
        assert is_new is True

    def test_update_existing(self, db):
        listing = _make_listing()
        db.upsert_listing(listing)
        db.conn.commit()

        listing2 = _make_listing(current_price=Decimal("1.30"))
        is_new = db.upsert_listing(listing2)
        assert is_new is False

    def test_different_conditions_are_separate(self, db):
        nm = _make_listing(condition="Near Mint")
        lp = _make_listing(condition="Lightly Played")
        assert db.upsert_listing(nm) is True
        assert db.upsert_listing(lp) is True
        db.conn.commit()

        active = db.get_all_active()
        assert len(active) == 2

    def test_dfc_front_face_dedups_to_existing_full_form(self, db):
        full = _make_listing(product_name="Grave Researcher // Reanimate")
        front = _make_listing(product_name="Grave Researcher")
        assert db.upsert_listing(full) is True
        assert db.upsert_listing(front) is False
        db.conn.commit()

        active = db.get_all_active()
        assert len(active) == 1
        # Richer name preserved — not downgraded to front-face form.
        assert active[0]["product_name"] == "Grave Researcher // Reanimate"

    def test_dfc_full_form_upgrades_existing_front_face(self, db):
        front = _make_listing(product_name="Grave Researcher")
        full = _make_listing(product_name="Grave Researcher // Reanimate")
        assert db.upsert_listing(front) is True
        assert db.upsert_listing(full) is False
        db.conn.commit()

        active = db.get_all_active()
        assert len(active) == 1
        assert active[0]["product_name"] == "Grave Researcher // Reanimate"

    def test_parenthetical_printings_stay_distinct(self, db):
        # "(Borderless)" is a separate printing — must NOT collapse into
        # the base printing during upsert dedup.
        base = _make_listing(product_name="Hop to It")
        borderless = _make_listing(product_name="Hop to It (Borderless)")
        assert db.upsert_listing(base) is True
        assert db.upsert_listing(borderless) is True
        db.conn.commit()

        active = db.get_all_active()
        assert len(active) == 2


class TestGetAllActive:
    def test_returns_active_only(self, db):
        db.upsert_listing(_make_listing(product_name="Active"))
        db.upsert_listing(_make_listing(product_name="Also Active"))
        db.conn.commit()

        active = db.get_all_active()
        assert len(active) == 2

    def test_empty_db(self, db):
        assert db.get_all_active() == []


class TestGetSummary:
    def test_summary_stats(self, db):
        db.upsert_listing(
            _make_listing(product_name="A", current_price=Decimal("5.00"), quantity=2)
        )
        db.upsert_listing(
            _make_listing(product_name="B", current_price=Decimal("3.00"), quantity=1)
        )
        db.conn.commit()

        summary = db.get_summary()
        assert summary["active_listings"] == 2
        assert summary["total_quantity"] == 3
        assert abs(summary["total_value"] - 13.0) < 0.01  # 5*2 + 3*1

    def test_empty_summary(self, db):
        summary = db.get_summary()
        assert summary["active_listings"] == 0
        assert summary["total_value"] == 0.0


class TestIsListed:
    def test_not_listed(self, db):
        assert db.is_listed("Card A", "Near Mint") is False

    def test_listed(self, db):
        db.upsert_listing(_make_listing(product_name="Card A"))
        db.conn.commit()
        assert db.is_listed("Card A", "Near Mint") is True

    def test_different_condition_not_listed(self, db):
        db.upsert_listing(_make_listing(product_name="Card A", condition="Near Mint"))
        db.conn.commit()
        assert db.is_listed("Card A", "Lightly Played") is False

    def test_sold_not_listed(self, db):
        db.sync([_make_listing(product_name="Card A")])
        db.sync([])  # Sell it
        assert db.is_listed("Card A", "Near Mint") is False


class TestIsListedFuzzy:
    def test_exact_match(self, db):
        db.upsert_listing(_make_listing(product_name="Lightning Bolt"))
        db.conn.commit()
        assert db.is_listed_fuzzy("Lightning Bolt", "Near Mint") is True

    def test_partial_match(self, db):
        db.upsert_listing(_make_listing(product_name="Delver of Secrets (2367)"))
        db.conn.commit()
        assert db.is_listed_fuzzy("Delver of Secrets", "Near Mint") is True

    def test_double_faced_match(self, db):
        db.upsert_listing(_make_listing(product_name="Delver of Secrets (2367)"))
        db.conn.commit()
        assert db.is_listed_fuzzy("Delver of Secrets // Insectile Aberration", "Near Mint") is True

    def test_no_match(self, db):
        db.upsert_listing(_make_listing(product_name="Lightning Bolt"))
        db.conn.commit()
        assert db.is_listed_fuzzy("Totally Different Card", "Near Mint") is False

    def test_foil_finish_matters(self, db):
        db.upsert_listing(_make_listing(product_name="Sol Ring"), finish="Foil")
        db.conn.commit()
        assert db.is_listed_fuzzy("Sol Ring", "Near Mint", "Foil") is True
        assert db.is_listed_fuzzy("Sol Ring", "Near Mint", "") is False


class TestSync:
    def test_first_sync_adds_all(self, db):
        listings = [
            _make_listing(product_name="Card A"),
            _make_listing(product_name="Card B"),
        ]
        report = db.sync(listings)
        assert report.total_active == 2
        assert len(report.added) == 2
        assert len(report.removed) == 0

    def test_no_changes(self, db):
        listings = [_make_listing(product_name="Card A")]
        db.sync(listings)

        # Sync again with same data
        report = db.sync(listings)
        assert report.total_active == 1
        assert len(report.added) == 0
        assert len(report.removed) == 0
        assert report.has_changes is False

    def test_detects_sold(self, db):
        # First sync with 2 cards
        db.sync(
            [
                _make_listing(product_name="Card A"),
                _make_listing(product_name="Card B"),
            ]
        )

        # Second sync with only 1 card — Card B was sold
        report = db.sync([_make_listing(product_name="Card A")])
        assert report.total_active == 1
        assert len(report.removed) == 1
        assert "Card B" in report.removed[0]

    def test_detects_new(self, db):
        db.sync([_make_listing(product_name="Card A")])

        # Add a new card
        report = db.sync(
            [
                _make_listing(product_name="Card A"),
                _make_listing(product_name="Card C"),
            ]
        )
        assert len(report.added) == 1
        assert "Card C" in report.added[0]

    def test_detects_price_change(self, db):
        db.sync([_make_listing(product_name="Card A", current_price=Decimal("5.00"))])

        report = db.sync([_make_listing(product_name="Card A", current_price=Decimal("4.00"))])
        assert len(report.price_changed) == 1
        assert report.price_changed[0]["old_price"] == 5.0
        assert report.price_changed[0]["new_price"] == 4.0

    def test_detects_quantity_change(self, db):
        db.sync([_make_listing(product_name="Card A", quantity=3)])

        report = db.sync([_make_listing(product_name="Card A", quantity=2)])
        assert len(report.quantity_changed) == 1
        assert report.quantity_changed[0]["old_qty"] == 3
        assert report.quantity_changed[0]["new_qty"] == 2

    def test_removed_then_relisted(self, db):
        """Missing from a sync means 'removed' (may be sold, delisted, or a
        scrape glitch). True 'sold' only comes from `record_order_sales`."""
        db.sync([_make_listing(product_name="Card A")])
        db.sync([])  # Card A disappeared from scrape

        # Should be 'removed', not 'sold'
        assert len(db.get_all_sold()) == 0
        removed = db.conn.execute("SELECT * FROM inventory WHERE status = 'removed'").fetchall()
        assert len(removed) == 1

        # Re-list it
        report = db.sync([_make_listing(product_name="Card A")])
        assert report.total_active == 1
        assert len(report.added) == 0  # Not "new" — it already existed
        removed_after = db.conn.execute(
            "SELECT COUNT(*) as c FROM inventory WHERE status = 'removed'"
        ).fetchone()
        assert removed_after["c"] == 0  # No longer removed

    def test_sync_log(self, db):
        db.sync([_make_listing(product_name="A")])
        db.sync([_make_listing(product_name="A"), _make_listing(product_name="B")])

        logs = db.conn.execute("SELECT * FROM sync_log ORDER BY id").fetchall()
        assert len(logs) == 2
        assert logs[0]["added"] == 1
        assert logs[1]["added"] == 1


class TestMetadata:
    def test_get_missing_key_returns_none(self, db):
        assert db.get_metadata("nonexistent") is None

    def test_set_and_get_roundtrip(self, db):
        db.set_metadata("last_tcg_scrape", "2026-01-01T00:00:00")
        assert db.get_metadata("last_tcg_scrape") == "2026-01-01T00:00:00"

    def test_set_overwrites_existing(self, db):
        db.set_metadata("k", "v1")
        db.set_metadata("k", "v2")
        assert db.get_metadata("k") == "v2"


class TestUpdateTcgPrice:
    def test_updates_matching_active_row(self, db):
        db.upsert_listing(
            _make_listing(product_name="Bolt", condition="Near Mint", current_price=Decimal("2.00"))
        )
        rows = db.update_tcg_price("Bolt", "Near Mint", 1.50)
        assert rows == 1
        row = db.conn.execute(
            "SELECT current_price FROM inventory WHERE product_name='Bolt'"
        ).fetchone()
        assert row["current_price"] == pytest.approx(1.50)

    def test_no_match_returns_zero(self, db):
        assert db.update_tcg_price("Ghost Card", "Near Mint", 1.00) == 0

    def test_does_not_touch_inactive_rows(self, db):
        db.upsert_listing(
            _make_listing(product_name="Sold", condition="Near Mint", current_price=Decimal("2.00"))
        )
        db.conn.execute("UPDATE inventory SET status='inactive' WHERE product_name='Sold'")
        db.conn.commit()
        rows = db.update_tcg_price("Sold", "Near Mint", 0.50)
        assert rows == 0
        row = db.conn.execute(
            "SELECT current_price FROM inventory WHERE product_name='Sold'"
        ).fetchone()
        assert row["current_price"] == pytest.approx(2.00)
