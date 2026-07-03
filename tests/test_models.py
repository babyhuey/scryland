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


class TestReportTimestampNotFrozenAtImport:
    """`timestamp: datetime = datetime.now(UTC)` is a mutable default —
    it's evaluated once at class-definition time and shared by every
    instance. It must be a default_factory so each instance gets its own
    fresh timestamp. Verified by spying on datetime.now rather than
    sleeping between instantiations (which would be flaky either way)."""

    def _spy(self, monkeypatch):
        import scryland.models as models_mod

        calls: list[None] = []
        real_now = models_mod.datetime.now

        class SpyDatetime(models_mod.datetime):
            @classmethod
            def now(cls, tz=None):
                calls.append(None)
                return real_now(tz)

        monkeypatch.setattr(models_mod, "datetime", SpyDatetime)
        return calls

    def test_pricing_report_timestamp_computed_per_instance(self, monkeypatch):
        calls = self._spy(monkeypatch)
        r1 = PricingReport()
        r2 = PricingReport()
        assert len(calls) == 2
        assert r1.timestamp is not r2.timestamp

    def test_sync_report_timestamp_computed_per_instance(self, monkeypatch):
        from scryland.models import SyncReport

        calls = self._spy(monkeypatch)
        s1 = SyncReport()
        s2 = SyncReport()
        assert len(calls) == 2
        assert s1.timestamp is not s2.timestamp
