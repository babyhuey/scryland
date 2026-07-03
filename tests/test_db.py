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


class TestIsKnown:
    def test_returns_none_when_unknown(self, db):
        assert db.is_known("Ghost Card", "Near Mint", "") is None

    def test_bare_condition_matches_foil_row_synced_with_embedded_condition(self, db):
        """sync() stores the scraped condition with the finish embedded
        ("Near Mint Foil"), while add-inventory looks it up with the bare
        condition ("Near Mint") plus a separate finish ("Foil") — these must
        still match so foil cards dedup correctly."""
        db.sync([_make_listing(product_name="Sol Ring", condition="Near Mint Foil")])
        assert db.is_known("Sol Ring", "Near Mint", "Foil") == "active"

    def test_non_foil_still_matches(self, db):
        db.sync([_make_listing(product_name="Sol Ring", condition="Near Mint")])
        assert db.is_known("Sol Ring", "Near Mint", "") == "active"


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

    def test_bare_condition_matches_row_synced_with_embedded_foil_condition(self, db):
        """Same add-inventory-vs-sync() mismatch as TestIsKnown, but for the
        fuzzy path add-inventory also calls."""
        db.sync([_make_listing(product_name="Sol Ring", condition="Near Mint Foil")])
        assert db.is_listed_fuzzy("Sol Ring", "Near Mint", "Foil") is True


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


class TestIsSaleRecorded:
    def test_false_before_recording(self, db):
        assert db.is_sale_recorded("ORD-1", "Bolt", "Near Mint", "ebay") is False

    def test_true_after_recording(self, db):
        db.record_order_sales(
            [
                {
                    "order_number": "ORD-1",
                    "product_name": "Bolt",
                    "condition": "Near Mint",
                    "_marketplace": "ebay",
                }
            ]
        )
        assert db.is_sale_recorded("ORD-1", "Bolt", "Near Mint", "ebay") is True

    def test_scoped_to_marketplace(self, db):
        db.record_order_sales(
            [
                {
                    "order_number": "ORD-1",
                    "product_name": "Bolt",
                    "condition": "Near Mint",
                    "_marketplace": "tcgplayer",
                }
            ]
        )
        assert db.is_sale_recorded("ORD-1", "Bolt", "Near Mint", "ebay") is False


class TestInsertSaleRowMarketplaceDedup:
    def test_same_order_different_marketplace_both_recorded(self, db):
        """TCG and eBay sales for the same order/card must both be stored."""
        tcg_sale = {
            "order_number": "ORD-001",
            "product_name": "Lightning Bolt",
            "condition": "Near Mint",
            "_marketplace": "tcgplayer",
        }
        ebay_sale = {
            "order_number": "ORD-001",
            "product_name": "Lightning Bolt",
            "condition": "Near Mint",
            "_marketplace": "ebay",
        }
        n1 = db.record_order_sales([tcg_sale])
        n2 = db.record_order_sales([ebay_sale])
        assert n1 == 1
        assert n2 == 1
        rows = db.conn.execute("SELECT marketplace FROM sales ORDER BY id").fetchall()
        assert [r["marketplace"] for r in rows] == ["tcgplayer", "ebay"]

    def test_same_marketplace_duplicate_not_double_inserted(self, db):
        """Running twice for the same sale returns 0 on the second call."""
        sale = {
            "order_number": "ORD-002",
            "product_name": "Bolt",
            "condition": "NM",
            "_marketplace": "tcgplayer",
        }
        assert db.record_order_sales([sale]) == 1
        assert db.record_order_sales([sale]) == 0


class TestRecordSaleMarketplace:
    def test_record_sale_preserves_marketplace(self, db):
        """record_sale must store the _marketplace key from the sale dict."""
        sale = {
            "order_number": "ORD-003",
            "product_name": "Bolt",
            "condition": "NM",
            "_marketplace": "ebay",
        }
        result = db.record_sale(sale)
        assert result is True
        row = db.conn.execute(
            "SELECT marketplace FROM sales WHERE order_number = 'ORD-003'"
        ).fetchone()
        assert row["marketplace"] == "ebay"


class TestNullConditionGuard:
    def test_find_inventory_by_canonical_tolerates_null_condition(self, db):
        """find_inventory_by_canonical must not crash when condition is NULL."""
        from scryland.db import canonical_key

        db.conn.execute(
            "INSERT INTO inventory (product_name, condition, finish, status, current_price, quantity, first_seen, last_seen) "
            "VALUES ('Test Card', NULL, '', 'active', 1.00, 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        db.conn.commit()
        key = canonical_key("Test Card", "Near Mint", False)
        result = db.find_inventory_by_canonical(key)
        assert result is None  # no match, but no crash


class TestFalsyZeroPriceTracking:
    def test_upsert_listing_records_change_from_zero(self, db):
        """A listing with price 0.0 that gets repriced should set last_price_change."""
        listing = _make_listing(
            product_name="Bolt", condition="Near Mint", current_price=Decimal("0.00")
        )
        db.upsert_listing(listing, "")
        db.conn.execute("UPDATE inventory SET current_price = 0.0 WHERE product_name = 'Bolt'")
        db.conn.commit()

        repriced = _make_listing(
            product_name="Bolt", condition="Near Mint", current_price=Decimal("1.50")
        )
        db.upsert_listing(repriced, "")

        row = db.conn.execute(
            "SELECT last_price_change FROM inventory WHERE product_name = 'Bolt'"
        ).fetchone()
        assert row["last_price_change"] is not None

    def test_sync_counts_change_from_zero(self, db):
        """sync() must count a $0→$X reprice in price_changed."""
        listing = _make_listing(
            product_name="Bolt", condition="Near Mint", current_price=Decimal("0.00")
        )
        db.upsert_listing(listing, "")
        db.conn.execute(
            "UPDATE inventory SET current_price = 0.0, status = 'active' WHERE product_name = 'Bolt'"
        )
        db.conn.commit()

        repriced = _make_listing(
            product_name="Bolt", condition="Near Mint", current_price=Decimal("1.50")
        )
        report = db.sync([repriced])
        assert len(report.price_changed) == 1
        assert report.price_changed[0]["old_price"] == pytest.approx(0.0)
        assert report.price_changed[0]["new_price"] == pytest.approx(1.50)


class TestTcgLowPriceFalsyZeroAndPreserve:
    """`float(x) if x else None` treats Decimal("0") as missing, and a
    plain UPDATE overwrites a known tcg_low_price with NULL when a later
    scrape lacks it."""

    def test_zero_price_stored_as_zero_not_null_on_insert(self, db):
        listing = _make_listing(
            product_name="Bolt", condition="Near Mint", tcg_low_price=Decimal("0")
        )
        db.upsert_listing(listing, "")
        db.conn.commit()
        row = db.conn.execute(
            "SELECT tcg_low_price FROM inventory WHERE product_name = 'Bolt'"
        ).fetchone()
        assert row["tcg_low_price"] == pytest.approx(0.0)

    def test_zero_price_stored_as_zero_not_null_on_update(self, db):
        listing = _make_listing(
            product_name="Bolt", condition="Near Mint", tcg_low_price=Decimal("1.00")
        )
        db.upsert_listing(listing, "")
        db.conn.commit()

        repriced = _make_listing(
            product_name="Bolt", condition="Near Mint", tcg_low_price=Decimal("0")
        )
        db.upsert_listing(repriced, "")
        row = db.conn.execute(
            "SELECT tcg_low_price FROM inventory WHERE product_name = 'Bolt'"
        ).fetchone()
        assert row["tcg_low_price"] == pytest.approx(0.0)

    def test_update_with_none_preserves_prior_tcg_low_price(self, db):
        listing = _make_listing(
            product_name="Bolt", condition="Near Mint", tcg_low_price=Decimal("2.50")
        )
        db.upsert_listing(listing, "")
        db.conn.commit()

        rescraped = _make_listing(
            product_name="Bolt", condition="Near Mint", tcg_low_price=None
        )
        db.upsert_listing(rescraped, "")
        row = db.conn.execute(
            "SELECT tcg_low_price FROM inventory WHERE product_name = 'Bolt'"
        ).fetchone()
        assert row["tcg_low_price"] == pytest.approx(2.50)

    def test_record_price_stores_zero_tcg_low_not_null(self, db):
        listing = _make_listing(
            product_name="Bolt", condition="Near Mint", tcg_low_price=Decimal("0")
        )
        db.record_price(listing)
        db.conn.commit()
        row = db.conn.execute(
            "SELECT tcg_low FROM price_history WHERE product_name = 'Bolt'"
        ).fetchone()
        assert row["tcg_low"] == pytest.approx(0.0)


class TestIsListedFuzzyLikeEscape:
    def test_underscore_in_name_does_not_match_wildcard(self, db):
        """A card name with _ should not match a card with a different character there."""
        db.upsert_listing(
            _make_listing(
                product_name="Vessel of Nascency",
                condition="Near Mint",
                current_price=Decimal("0.50"),
            ),
            "",
        )
        assert not db.is_listed_fuzzy("Vessel_of_Nascency", "Near Mint", "")

    def test_percent_in_name_does_not_expand(self, db):
        """A card name with % should not match anything-goes wildcard expansion."""
        db.upsert_listing(
            _make_listing(
                product_name="Some Card", condition="Near Mint", current_price=Decimal("1.00")
            ),
            "",
        )
        assert not db.is_listed_fuzzy("Some%", "Near Mint", "")


class TestSyncTransactionSafety:
    def test_sync_rolls_back_on_upsert_failure(self, db, monkeypatch):
        """If sync() raises mid-loop, inventory rows must remain 'active', not stuck in 'checking'."""
        listing = _make_listing(
            product_name="Bolt", condition="Near Mint", current_price=Decimal("1.00")
        )
        db.upsert_listing(listing, "")
        db.conn.commit()

        def failing_upsert(lst, finish):
            raise RuntimeError("simulated upsert failure")

        monkeypatch.setattr(db, "upsert_listing", failing_upsert)

        with pytest.raises(RuntimeError):
            db.sync([listing])

        row = db.conn.execute("SELECT status FROM inventory WHERE product_name = 'Bolt'").fetchone()
        assert row["status"] == "active"


class TestMarkInventorySoldPrecision:
    """_mark_inventory_sold must not over-match: exact name (case-insensitive),
    constrained by condition/finish when known — not a `%name%` substring
    scan across every condition/finish of a card."""

    def test_nm_sale_does_not_mark_other_conditions_sold(self, db):
        db.sync(
            [
                _make_listing(product_name="Sol Ring", condition="Near Mint"),
                _make_listing(product_name="Sol Ring", condition="Lightly Played"),
                _make_listing(product_name="Sol Ring", condition="Near Mint Foil"),
            ]
        )
        db.record_order_sales(
            [{"order_number": "ORD-1", "product_name": "Sol Ring", "condition": "Near Mint"}]
        )
        rows = {
            r["condition"]: r["status"]
            for r in db.conn.execute(
                "SELECT condition, status FROM inventory WHERE product_name = 'Sol Ring'"
            ).fetchall()
        }
        assert rows["Near Mint"] == "sold"
        assert rows["Lightly Played"] == "active"
        assert rows["Near Mint Foil"] == "active"

    def test_island_sale_does_not_mark_island_of_wak_wak(self, db):
        db.sync(
            [
                _make_listing(product_name="Island", condition="Near Mint"),
                _make_listing(product_name="Island of Wak-Wak", condition="Near Mint"),
            ]
        )
        db.record_order_sales(
            [{"order_number": "ORD-2", "product_name": "Island", "condition": "Near Mint"}]
        )
        rows = {
            r["product_name"]: r["status"]
            for r in db.conn.execute(
                "SELECT product_name, status FROM inventory "
                "WHERE product_name IN ('Island', 'Island of Wak-Wak')"
            ).fetchall()
        }
        assert rows["Island"] == "sold"
        assert rows["Island of Wak-Wak"] == "active"


class TestFindInventoryByCanonicalIncludeRemoved:
    def test_active_found_without_flag(self, db):
        from scryland.db import canonical_key

        db.conn.execute(
            "INSERT INTO inventory (product_name, condition, finish, status, current_price, quantity, first_seen, last_seen) "
            "VALUES ('Fireball', 'Near Mint', '', 'active', 1.00, 1, '2026-01-01', '2026-01-01')"
        )
        db.conn.commit()
        key = canonical_key("Fireball", "Near Mint", False)
        assert db.find_inventory_by_canonical(key) is not None

    def test_removed_not_found_by_default(self, db):
        from scryland.db import canonical_key

        db.conn.execute(
            "INSERT INTO inventory (product_name, condition, finish, status, current_price, quantity, first_seen, last_seen) "
            "VALUES ('Fireball', 'Near Mint', '', 'removed', 1.00, 1, '2026-01-01', '2026-01-01')"
        )
        db.conn.commit()
        key = canonical_key("Fireball", "Near Mint", False)
        assert db.find_inventory_by_canonical(key) is None

    def test_removed_found_with_include_removed(self, db):
        from scryland.db import canonical_key

        db.conn.execute(
            "INSERT INTO inventory (product_name, condition, finish, status, current_price, quantity, first_seen, last_seen) "
            "VALUES ('Fireball', 'Near Mint', '', 'removed', 1.00, 1, '2026-01-01', '2026-01-01')"
        )
        db.conn.commit()
        key = canonical_key("Fireball", "Near Mint", False)
        assert db.find_inventory_by_canonical(key, include_removed=True) is not None


class TestReclassifyFalseSoldLikeEscape:
    """_reclassify_false_sold concatenates product names into a LIKE
    pattern without escaping '%'/'_' — a name containing '%' turns into a
    wildcard, over-matching a sale that doesn't really correspond and
    incorrectly leaving the row 'sold' instead of reclassifying it."""

    def test_percent_in_name_does_not_over_match(self, db):
        now = "2026-01-01T00:00:00"
        db.conn.execute(
            "INSERT INTO inventory (product_name, condition, finish, status, "
            "current_price, quantity, first_seen, last_seen) "
            "VALUES ('100% Foil Promo', 'Near Mint', '', 'sold', 1.00, 1, ?, ?)",
            (now, now),
        )
        # Does NOT literally contain "100% Foil Promo" — would only match
        # if the '%' in the inventory name were (incorrectly) treated as a
        # SQL wildcard instead of a literal character.
        db.conn.execute(
            "INSERT INTO sales (order_number, order_date, product_name, condition, recorded_at) "
            "VALUES ('X', ?, '100XXXXXX Foil Promo', 'Near Mint', ?)",
            (now, now),
        )
        db.conn.commit()

        db._reclassify_false_sold()

        row = db.conn.execute(
            "SELECT status FROM inventory WHERE product_name = '100% Foil Promo'"
        ).fetchone()
        assert row["status"] == "removed"
