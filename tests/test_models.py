"""Tests for domain models."""

from decimal import Decimal

from scryland.models import Listing, PriceUpdate, PricingReport, UpdateStatus


class TestListing:
    def test_best_comparison_price_prefers_tcg_low(self):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=Decimal("4.00"),
            tcg_low_with_shipping=Decimal("4.50"),
            market_price=Decimal("4.75"),
        )
        assert listing.best_comparison_price == Decimal("4.00")

    def test_best_comparison_price_falls_back_to_shipping(self):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=None,
            tcg_low_with_shipping=Decimal("4.50"),
            market_price=Decimal("4.75"),
        )
        assert listing.best_comparison_price == Decimal("4.50")

    def test_best_comparison_price_falls_back_to_market(self):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=None,
            tcg_low_with_shipping=None,
            market_price=Decimal("4.75"),
        )
        assert listing.best_comparison_price == Decimal("4.75")

    def test_best_comparison_price_none_when_no_data(self):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
        )
        assert listing.best_comparison_price is None


class TestPriceUpdate:
    def test_change_direction_decrease(self):
        update = PriceUpdate(
            listing=Listing(product_name="Test", current_price=Decimal("5.00")),
            new_price=Decimal("4.00"),
            old_price=Decimal("5.00"),
            change_pct=-20.0,
        )
        assert update.change_direction == "decrease"

    def test_change_direction_increase(self):
        update = PriceUpdate(
            listing=Listing(product_name="Test", current_price=Decimal("5.00")),
            new_price=Decimal("6.00"),
            old_price=Decimal("5.00"),
            change_pct=20.0,
        )
        assert update.change_direction == "increase"

    def test_change_direction_none(self):
        update = PriceUpdate(
            listing=Listing(product_name="Test", current_price=Decimal("5.00")),
            new_price=Decimal("5.00"),
            old_price=Decimal("5.00"),
            change_pct=0.0,
        )
        assert update.change_direction == "none"

    def test_status_transitions(self):
        update = PriceUpdate(
            listing=Listing(product_name="Test", current_price=Decimal("5.00")),
            new_price=Decimal("4.00"),
            old_price=Decimal("5.00"),
            change_pct=-20.0,
        )
        assert update.status == UpdateStatus.PENDING

        update.approve()
        assert update.status == UpdateStatus.APPROVED

        update.mark_applied()
        assert update.status == UpdateStatus.APPLIED

    def test_reject(self):
        update = PriceUpdate(
            listing=Listing(product_name="Test", current_price=Decimal("5.00")),
            new_price=Decimal("4.00"),
            old_price=Decimal("5.00"),
            change_pct=-20.0,
        )
        update.reject()
        assert update.status == UpdateStatus.REJECTED

    def test_mark_failed(self):
        update = PriceUpdate(
            listing=Listing(product_name="Test", current_price=Decimal("5.00")),
            new_price=Decimal("4.00"),
            old_price=Decimal("5.00"),
            change_pct=-20.0,
        )
        update.mark_failed()
        assert update.status == UpdateStatus.FAILED


class TestPricingReport:
    def test_defaults(self):
        report = PricingReport()
        assert report.total_listings == 0
        assert report.updates_proposed == 0
        assert report.updates_applied == 0
        assert report.dry_run is False
        assert report.updates == []
