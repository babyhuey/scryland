"""Tests for price comparison logic."""

from decimal import Decimal

import pytest

from scryland.config import ScrylandConfig
from scryland.models import Listing
from scryland.pricing.comparator import PriceComparator


@pytest.fixture
def comparator(config):
    return PriceComparator(config)


class TestComputeOptimalPrice:
    def test_suggests_tcg_low_when_lower(self, comparator):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=Decimal("4.00"),
        )
        assert comparator.compute_optimal_price(listing) == Decimal("4.00")

    def test_no_change_when_already_at_lowest(self, comparator):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("4.00"),
            tcg_low_price=Decimal("4.00"),
        )
        assert comparator.compute_optimal_price(listing) is None

    def test_no_change_when_below_lowest(self, comparator):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("3.50"),
            tcg_low_price=Decimal("4.00"),
        )
        assert comparator.compute_optimal_price(listing) is None

    def test_enforces_price_floor(self, comparator):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("0.50"),
            tcg_low_price=Decimal("0.10"),
        )
        # tcg_low is $0.10 but floor is $0.25, so suggest $0.25 (not $0.10)
        assert comparator.compute_optimal_price(listing) == Decimal("0.25")

    def test_no_change_when_already_at_floor(self, comparator):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("0.25"),
            tcg_low_price=Decimal("0.10"),
        )
        # Already at floor, no change needed
        assert comparator.compute_optimal_price(listing) is None

    def test_floor_used_when_target_below_floor(self):
        config = ScrylandConfig(min_price_floor=1.00)
        comparator = PriceComparator(config)
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=Decimal("0.50"),
        )
        # Target would be $0.50 but floor is $1.00, and current is $5.00 > $1.00
        assert comparator.compute_optimal_price(listing) == Decimal("1.00")

    def test_returns_none_when_no_comparison_data(self, comparator):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=None,
            market_price=None,
        )
        assert comparator.compute_optimal_price(listing) is None

    def test_uses_market_price_as_fallback(self, comparator):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=None,
            tcg_low_with_shipping=None,
            market_price=Decimal("4.00"),
        )
        assert comparator.compute_optimal_price(listing) == Decimal("4.00")

    def test_uses_tcg_low_with_shipping_as_fallback(self, comparator):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=None,
            tcg_low_with_shipping=Decimal("4.50"),
            market_price=Decimal("4.00"),
        )
        assert comparator.compute_optimal_price(listing) == Decimal("4.50")


class TestComputeChangePct:
    def test_decrease(self, comparator):
        result = comparator.compute_change_pct(Decimal("10.00"), Decimal("8.00"))
        assert result == pytest.approx(-20.0)

    def test_increase(self, comparator):
        result = comparator.compute_change_pct(Decimal("10.00"), Decimal("12.00"))
        assert result == pytest.approx(20.0)

    def test_no_change(self, comparator):
        result = comparator.compute_change_pct(Decimal("10.00"), Decimal("10.00"))
        assert result == pytest.approx(0.0)

    def test_zero_old_price_zero_new(self, comparator):
        result = comparator.compute_change_pct(Decimal("0"), Decimal("0"))
        assert result == 0.0

    def test_zero_old_price_nonzero_new(self, comparator):
        result = comparator.compute_change_pct(Decimal("0"), Decimal("5.00"))
        assert result == 100.0

    def test_small_change(self, comparator):
        result = comparator.compute_change_pct(Decimal("1.00"), Decimal("0.95"))
        assert result == pytest.approx(-5.0)
