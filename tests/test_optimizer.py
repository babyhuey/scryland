"""Tests for the shared price-differential optimize flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from scryland.config import ScrylandConfig
from scryland.pricing.optimizer import run_price_differential_optimize


@pytest.fixture
def config():
    return ScrylandConfig(dry_run=False, max_price_change_pct=10.0)


@pytest.fixture
def console():
    # Rich console in record-mode; no actual terminal output during tests.
    return Console(quiet=True, record=True)


def _session_mock():
    s = MagicMock()
    s.human_delay = AsyncMock()
    s.dismiss_popups = AsyncMock()
    s.page = MagicMock()
    return s


def _patch_pages(monkeypatch, differentials=None, click_result=True):
    """Replace PriceReportPage + PricingPage with mocks."""
    import scryland.pricing.optimizer as opt

    report = MagicMock()
    report.navigate = AsyncMock()
    report.get_differentials = AsyncMock(return_value=differentials or [])
    report.click_manage_for_row = AsyncMock(return_value=click_result)
    report.go_back_to_report = AsyncMock()

    pricing = MagicMock()
    pricing.apply_match_lowest = AsyncMock()
    pricing.set_quantity_zero = AsyncMock()
    pricing.save_changes = AsyncMock()

    monkeypatch.setattr(opt, "PriceReportPage", lambda p, c: report)
    monkeypatch.setattr(opt, "PricingPage", lambda p, c: pricing)
    return report, pricing


class TestEmptyReport:
    async def test_no_differentials_returns_zero(self, config, console, monkeypatch):
        _patch_pages(monkeypatch, differentials=[])
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.total == 0
        assert result.updated == 0


class TestPriceIncrease:
    async def test_auto_applies_without_prompt(self, config, console, monkeypatch):
        _, pricing = _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Card A",
                    "condition": "Near Mint",
                    "marketplace_price": 1.00,
                    "lowest_listing": 2.00,
                    "pct_differential": "+100%",
                }
            ],
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.updated == 1
        assert result.total_change == pytest.approx(1.00)
        pricing.apply_match_lowest.assert_awaited_once_with("Near Mint")
        pricing.save_changes.assert_awaited_once()


class TestPriceDrop:
    async def test_small_drop_auto_applies(self, config, console, monkeypatch):
        _, pricing = _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Card A",
                    "condition": "Near Mint",
                    "marketplace_price": 2.00,
                    "lowest_listing": 1.90,  # -5%, under threshold
                    "pct_differential": "-5%",
                }
            ],
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.updated == 1
        pricing.apply_match_lowest.assert_awaited()

    async def test_big_drop_skipped_in_dry_run(self, console, monkeypatch):
        config = ScrylandConfig(dry_run=True, max_price_change_pct=10.0)
        _, pricing = _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Card A",
                    "condition": "Near Mint",
                    "marketplace_price": 10.00,
                    "lowest_listing": 5.00,  # -50%, big drop
                    "pct_differential": "-50%",
                }
            ],
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.updated == 0
        assert result.skipped == 1
        pricing.apply_match_lowest.assert_not_called()


class TestPennyDelist:
    async def test_penny_delists_instead_of_matching(self, config, console, monkeypatch):
        _, pricing = _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Cheap Card",
                    "condition": "Near Mint",
                    "marketplace_price": 0.50,
                    "lowest_listing": 0.01,  # penny → delist
                    "pct_differential": "-98%",
                }
            ],
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.delisted == 1
        assert result.updated == 0
        pricing.set_quantity_zero.assert_awaited_once_with("Near Mint")
        pricing.apply_match_lowest.assert_not_called()


class TestPriceFloor:
    async def test_below_floor_skipped_not_applied(self, config, console, monkeypatch):
        """A TCG lowest above the $0.01 delist threshold but below the
        configured price floor must not be applied — neither matched nor
        delisted."""
        _, pricing = _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Cheap Card",
                    "condition": "Near Mint",
                    "marketplace_price": 1.00,
                    "lowest_listing": 0.10,  # above $0.01, below default floor $0.25
                    "pct_differential": "-90%",
                }
            ],
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.skipped == 1
        assert result.updated == 0
        assert result.delisted == 0
        pricing.apply_match_lowest.assert_not_called()
        pricing.set_quantity_zero.assert_not_called()

    async def test_at_or_above_floor_applies_normally(self, config, console, monkeypatch):
        _, pricing = _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Card A",
                    "condition": "Near Mint",
                    "marketplace_price": 1.00,
                    "lowest_listing": 0.95,  # above the default $0.25 floor, small drop
                    "pct_differential": "-5%",
                }
            ],
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.updated == 1
        pricing.apply_match_lowest.assert_awaited_once_with("Near Mint")


class TestSaveFailure:
    async def test_save_failure_increments_failed_count(self, config, console, monkeypatch):
        _, pricing = _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Card A",
                    "condition": "Near Mint",
                    "marketplace_price": 1.00,
                    "lowest_listing": 2.00,
                    "pct_differential": "+100%",
                }
            ],
        )
        pricing.save_changes = AsyncMock(side_effect=RuntimeError("eBay 500"))
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.failed == 1
        assert result.updated == 0


class TestCantFindManage:
    async def test_missing_manage_button_skips(self, config, console, monkeypatch):
        _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Card A",
                    "condition": "Near Mint",
                    "marketplace_price": 1.00,
                    "lowest_listing": 2.00,
                }
            ],
            click_result=False,  # couldn't find Manage
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.skipped == 1
        assert result.updated == 0


class TestZeroCurrentPrice:
    async def test_degenerate_zero_current_uses_zero_pct(
        self,
        config,
        console,
        monkeypatch,
    ):
        """If current price is 0, we should not crash on division-by-zero."""
        _, pricing = _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Zero Card",
                    "condition": "Near Mint",
                    "marketplace_price": 0.00,  # degenerate
                    "lowest_listing": 5.00,
                }
            ],
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        # Goes through as a small/normal change (pct=0) and applies.
        assert result.updated == 1


class TestDbPriceUpdate:
    async def test_updates_db_after_successful_match(self, config, console, monkeypatch):
        """After a successful match, update_tcg_price is called with the new price."""
        _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Card A",
                    "condition": "Near Mint",
                    "marketplace_price": 2.00,
                    "lowest_listing": 1.90,  # -5%, under the 10% guardrail threshold
                }
            ],
        )
        session = _session_mock()
        db = MagicMock()

        result = await run_price_differential_optimize(session, config, console, db=db)

        assert result.updated == 1
        db.update_tcg_price.assert_called_once_with("Card A", "Near Mint", 1.90)

    async def test_no_db_update_on_delist(self, config, console, monkeypatch):
        """Delist (lowest <= 0.01) does not call update_tcg_price."""
        _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Cheap Card",
                    "condition": "Near Mint",
                    "marketplace_price": 0.50,
                    "lowest_listing": 0.01,
                }
            ],
        )
        session = _session_mock()
        db = MagicMock()

        result = await run_price_differential_optimize(session, config, console, db=db)

        assert result.delisted == 1
        db.update_tcg_price.assert_not_called()

    async def test_no_db_kwarg_still_works(self, config, console, monkeypatch):
        """Omitting db= keeps existing behavior — no crash."""
        _patch_pages(
            monkeypatch,
            differentials=[
                {
                    "product_name": "Card A",
                    "condition": "Near Mint",
                    "marketplace_price": 1.00,
                    "lowest_listing": 1.50,
                }
            ],
        )
        session = _session_mock()

        result = await run_price_differential_optimize(session, config, console)

        assert result.updated == 1
