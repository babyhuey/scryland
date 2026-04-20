"""Tests for the pricing engine."""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from scryland.config import ScrylandConfig
from scryland.models import Listing
from scryland.pricing.comparator import PriceComparator
from scryland.pricing.engine import PricingEngine
from scryland.pricing.guardrails import PriceGuardrails


@pytest.fixture
def engine(config):
    return PricingEngine(
        config=config,
        comparator=PriceComparator(config),
        guardrails=PriceGuardrails(config),
    )


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.page = AsyncMock()
    return session


def _mock_inventory(products, listings_by_index):
    """Helper to create a mock InventoryPage.

    Args:
        products: list of {"name": ..., "set": ...} dicts
        listings_by_index: dict mapping product index to list of Listings
    """
    mock_inv = AsyncMock()
    mock_inv.get_product_names.return_value = products
    mock_inv.get_manage_page_listings = AsyncMock(
        side_effect=lambda name: listings_by_index.get(name, [])
    )
    mock_inv.click_manage_for_product = AsyncMock()
    return mock_inv


class TestProcessListing:
    def test_returns_update_when_price_can_be_lowered(self, engine):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
            tcg_low_price=Decimal("4.00"),
        )
        update = engine._process_listing(listing)
        assert update is not None
        assert update.new_price == Decimal("4.00")
        assert update.old_price == Decimal("5.00")

    def test_returns_none_when_already_optimal(self, engine):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("4.00"),
            tcg_low_price=Decimal("4.00"),
        )
        assert engine._process_listing(listing) is None

    def test_returns_none_when_no_comparison_data(self, engine):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("5.00"),
        )
        assert engine._process_listing(listing) is None

    def test_change_pct_is_calculated(self, engine):
        listing = Listing(
            product_name="Test",
            current_price=Decimal("10.00"),
            tcg_low_price=Decimal("8.00"),
        )
        update = engine._process_listing(listing)
        assert update is not None
        assert update.change_pct == pytest.approx(-20.0)


class TestRun:
    @pytest.mark.asyncio
    async def test_dry_run_no_changes_applied(self, mock_session):
        config = ScrylandConfig(dry_run=True)
        engine = PricingEngine(
            config=config,
            comparator=PriceComparator(config),
            guardrails=PriceGuardrails(config),
        )

        products = [{"name": "Test Card", "set": "Test Set"}]
        listings = [
            Listing(
                product_name="Test Card",
                condition="Near Mint",
                current_price=Decimal("5.00"),
                tcg_low_price=Decimal("4.50"),
                quantity=1,
            ),
        ]

        with (
            patch("scryland.pricing.engine.InventoryPage") as MockInventory,
            patch("scryland.pricing.engine.PricingPage") as MockPricing,
        ):
            mock_inv = _mock_inventory(products, {"Test Card": listings})
            MockInventory.return_value = mock_inv

            mock_pricing = AsyncMock()
            MockPricing.return_value = mock_pricing

            report, listings = await engine.run(mock_session)

        assert report.total_listings == 1
        assert report.updates_proposed == 1
        assert report.updates_applied == 0
        assert report.updates_skipped == 1
        assert report.dry_run is True
        mock_pricing.apply_price_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_already_optimal(self, mock_session):
        config = ScrylandConfig(dry_run=True)
        engine = PricingEngine(
            config=config,
            comparator=PriceComparator(config),
            guardrails=PriceGuardrails(config),
        )

        products = [{"name": "Already Optimal", "set": "Test Set"}]
        listings = [
            Listing(
                product_name="Already Optimal",
                condition="Near Mint",
                current_price=Decimal("4.00"),
                tcg_low_price=Decimal("4.00"),
                quantity=1,
            ),
        ]

        with (
            patch("scryland.pricing.engine.InventoryPage") as MockInventory,
            patch("scryland.pricing.engine.PricingPage"),
        ):
            mock_inv = _mock_inventory(products, {"Already Optimal": listings})
            MockInventory.return_value = mock_inv

            report, listings = await engine.run(mock_session)

        assert report.total_listings == 1
        assert report.updates_proposed == 0

    @pytest.mark.asyncio
    async def test_full_flow_applies_changes(self, mock_session):
        config = ScrylandConfig(dry_run=False, max_price_change_pct=10.0)
        engine = PricingEngine(
            config=config,
            comparator=PriceComparator(config),
            guardrails=PriceGuardrails(config),
        )

        products = [{"name": "Small Change", "set": "Test Set"}]
        listings = [
            Listing(
                product_name="Small Change",
                condition="Near Mint",
                current_price=Decimal("5.00"),
                tcg_low_price=Decimal("4.75"),  # 5% decrease — within threshold
                quantity=1,
            ),
        ]

        with (
            patch("scryland.pricing.engine.InventoryPage") as MockInventory,
            patch("scryland.pricing.engine.PricingPage") as MockPricing,
        ):
            mock_inv = _mock_inventory(products, {"Small Change": listings})
            MockInventory.return_value = mock_inv

            mock_pricing = AsyncMock()
            MockPricing.return_value = mock_pricing

            report, listings = await engine.run(mock_session)

        assert report.updates_applied == 1
        mock_pricing.apply_price_update.assert_called_once()
        mock_pricing.save_changes.assert_called_once()
