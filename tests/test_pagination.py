"""Tests for the shared pagination helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from scryland.browser.pagination import NextPageResult, click_next_page


class TestClickNextPage:
    async def test_last_page_returns_last_page(self):
        page = MagicMock()
        # evaluate returns "no-next" on the outer call (pagination),
        # but the helper passes through retry_on_flaky which calls
        # evaluate as a bare coroutine — so just mock it.
        page.evaluate = AsyncMock(return_value="no-next")
        page.wait_for_load_state = AsyncMock()
        result = await click_next_page(page)
        assert result is NextPageResult.LAST_PAGE

    async def test_advanced_when_load_settles(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="clicked")
        page.wait_for_load_state = AsyncMock()
        result = await click_next_page(page)
        assert result is NextPageResult.ADVANCED

    async def test_stalled_when_load_times_out(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="clicked")
        page.wait_for_load_state = AsyncMock(side_effect=Exception("timeout"))
        result = await click_next_page(page)
        assert result is NextPageResult.STALLED

    async def test_disabled_next_is_last_page(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="disabled")
        page.wait_for_load_state = AsyncMock()
        result = await click_next_page(page)
        assert result is NextPageResult.LAST_PAGE
