"""Shared test fixtures."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from scryland.config import ScrylandConfig
from scryland.models import Listing, PriceUpdate


@pytest.fixture
def config():
    """Default test configuration."""
    return ScrylandConfig(
        dry_run=True,
        headless=True,
        max_price_change_pct=10.0,
        min_price_floor=0.25,
    )


@pytest.fixture
def mock_page():
    """Mock Playwright Page object."""
    page = AsyncMock()
    page.url = "https://sellerportal.tcgplayer.com/inventory"
    return page


@pytest.fixture
def sample_listing():
    """A single listing for testing."""
    return Listing(
        product_name="Lightning Bolt",
        set_name="Fourth Edition",
        condition="Near Mint",
        printing="Normal",
        quantity=4,
        current_price=Decimal("1.50"),
        tcg_low_price=Decimal("1.20"),
        tcg_low_with_shipping=Decimal("1.30"),
        market_price=Decimal("1.35"),
    )


@pytest.fixture
def sample_listings():
    """A batch of diverse listings for testing various scenarios."""
    return [
        Listing(
            product_name="Lightning Bolt",
            set_name="Fourth Edition",
            condition="Near Mint",
            printing="Normal",
            quantity=4,
            current_price=Decimal("1.50"),
            tcg_low_price=Decimal("1.20"),
            market_price=Decimal("1.35"),
        ),
        Listing(
            product_name="Black Lotus",
            set_name="Unlimited",
            condition="Near Mint",
            printing="Normal",
            quantity=1,
            current_price=Decimal("50000.00"),
            tcg_low_price=Decimal("40000.00"),
            market_price=Decimal("45000.00"),
        ),
        Listing(
            product_name="Sol Ring",
            set_name="Commander 2021",
            condition="Near Mint",
            printing="Normal",
            quantity=10,
            current_price=Decimal("2.00"),
            tcg_low_price=Decimal("2.00"),
            market_price=Decimal("2.10"),
        ),
        Listing(
            product_name="Island",
            set_name="Core 2021",
            condition="Near Mint",
            printing="Normal",
            quantity=20,
            current_price=Decimal("0.50"),
            tcg_low_price=Decimal("0.10"),
            market_price=Decimal("0.15"),
        ),
        Listing(
            product_name="Rare Card",
            set_name="Mystery Set",
            condition="Near Mint",
            printing="Normal",
            quantity=1,
            current_price=Decimal("10.00"),
            tcg_low_price=None,
            market_price=None,
        ),
    ]


@pytest.fixture
def sample_update(sample_listing):
    """A sample price update."""
    return PriceUpdate(
        listing=sample_listing,
        new_price=Decimal("1.20"),
        old_price=Decimal("1.50"),
        change_pct=-20.0,
    )
