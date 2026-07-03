"""Tests for inventory page scraping."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from scryland.browser.pages.inventory import InventoryPage, _norm_name_for_match, _parse_price
from scryland.browser.selectors import Selectors
from scryland.config import ScrylandConfig


@pytest.fixture
def config():
    return ScrylandConfig(headless=True)


class TestNormNameForMatch:
    def test_collapses_dfc_to_front_face(self):
        assert _norm_name_for_match("Grave Researcher // Reanimate") == "grave researcher"
        assert _norm_name_for_match("Grave Researcher") == "grave researcher"

    def test_strips_parenthetical(self):
        assert _norm_name_for_match("Hop to It (Borderless)") == "hop to it"

    def test_case_and_punctuation(self):
        assert _norm_name_for_match("Forum's Favor!") == "forum s favor"


class TestParsePrice:
    def test_parses_dollar_amount(self):
        assert _parse_price("$1.50") == Decimal("1.50")

    def test_parses_without_dollar_sign(self):
        assert _parse_price("1.50") == Decimal("1.50")

    def test_parses_with_comma(self):
        assert _parse_price("$1,234.56") == Decimal("1234.56")

    def test_returns_none_for_empty(self):
        assert _parse_price("") is None
        assert _parse_price(None) is None

    def test_returns_none_for_dash(self):
        assert _parse_price("-") is None

    def test_returns_none_for_na(self):
        assert _parse_price("N/A") is None
        assert _parse_price("n/a") is None

    def test_returns_none_for_garbage(self):
        assert _parse_price("abc") is None

    def test_handles_whitespace(self):
        assert _parse_price("  $1.50  ") == Decimal("1.50")

    def test_parses_zero(self):
        assert _parse_price("$0.00") == Decimal("0.00")

    def test_parses_large_number(self):
        assert _parse_price("$50,000.00") == Decimal("50000.00")

    def test_parses_price_with_shipping(self):
        assert _parse_price("$20.34\n+ Shipping: $0.99") == Decimal("20.34")

    def test_parses_price_with_shipping_inline(self):
        assert _parse_price("$16.68 + Shipping: $5.99") == Decimal("16.68")


class TestVerifyProductAbsent:
    @pytest.mark.asyncio
    async def test_fills_search_box_with_raw_name_not_normalized(self, config):
        # Regression test: filling the search box with the punctuation-
        # stripped normalized name (e.g. "jace vryn s prodigy") could
        # return zero results even when the product IS listed under its
        # real name, inverting this safety check.
        page = MagicMock()
        search_input = AsyncMock()
        search_input.count = AsyncMock(return_value=1)
        search_btn = AsyncMock()

        def locator(selector):
            if selector == Selectors.SEARCH_INPUT:
                return search_input
            return search_btn

        page.locator = MagicMock(side_effect=locator)
        page.wait_for_load_state = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock(return_value=0)

        inv = InventoryPage(page, config)
        result = await inv.verify_product_absent("Jace, Vryn's Prodigy")

        search_input.fill.assert_awaited_once_with("Jace, Vryn's Prodigy")
        assert result is True

    @pytest.mark.asyncio
    async def test_present_when_manage_buttons_found(self, config):
        page = MagicMock()
        search_input = AsyncMock()
        search_input.count = AsyncMock(return_value=1)
        search_btn = AsyncMock()

        def locator(selector):
            if selector == Selectors.SEARCH_INPUT:
                return search_input
            return search_btn

        page.locator = MagicMock(side_effect=locator)
        page.wait_for_load_state = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.evaluate = AsyncMock(return_value=1)

        inv = InventoryPage(page, config)
        result = await inv.verify_product_absent("Jace, Vryn's Prodigy")

        assert result is False


class TestClickManageForProduct:
    @pytest.mark.asyncio
    async def test_best_score_wins_over_first_hit(self, config):
        # Regression test: the old single-pass evaluate clicked the first
        # matching tier it found in DOM order, even when a later row was an
        # exact match. Now all Manage rows are scored first, then the
        # highest-scoring row (idx=2, an exact match) is clicked instead of
        # an earlier partial match (idx=0).
        page = MagicMock()
        page.evaluate = AsyncMock(
            side_effect=[
                [{"idx": 0, "score": 1}, {"idx": 2, "score": 3}],
                True,
            ]
        )
        page.wait_for_load_state = AsyncMock()

        inv = InventoryPage(page, config)
        await inv.click_manage_for_product("Reprieve")

        click_call = page.evaluate.call_args_list[1]
        assert click_call.args[1] == 2
