"""Tests for the shared pagination helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from scryland.browser.pagination import NextPageResult, click_next_page


class TestClickNextPage:
    async def test_last_page_returns_last_page(self):
        page = MagicMock()
        # evaluate returns "no-next" on the outer call (pagination),
        # but the helper passes through retry_on_flaky which calls
        # evaluate as a bare coroutine — so just mock it.
        page.evaluate = AsyncMock(return_value="no-next")
        page.wait_for_function = AsyncMock()
        result = await click_next_page(page)
        assert result is NextPageResult.LAST_PAGE

    async def test_advanced_when_content_signature_changes(self):
        page = MagicMock()
        # First evaluate call captures the pre-click signature, second
        # performs the click.
        page.evaluate = AsyncMock(side_effect=["Viewing 1-25 of 100", "clicked"])
        page.wait_for_function = AsyncMock()
        result = await click_next_page(page)
        assert result is NextPageResult.ADVANCED
        # The captured "before" signature must be threaded through so the
        # predicate can detect a genuine change, not just networkidle.
        # wait_for_function's `arg` parameter is keyword-only in Playwright's
        # Python API — passing it positionally raises TypeError on every
        # real invocation.
        _args, kwargs = page.wait_for_function.call_args
        assert kwargs["arg"] == "Viewing 1-25 of 100"

    async def test_stalled_when_content_never_changes(self):
        # Regression test for the networkidle race: an already-idle page
        # resolves wait_for_load_state immediately, before the pagination
        # XHR fires, so a queued double-advance can skip a page. Now we
        # wait for the content signature to actually change; if it never
        # does within the timeout, that must surface as STALLED.
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=["Viewing 1-25 of 100", "clicked"])
        page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("Timeout 15000ms exceeded")
        )
        result = await click_next_page(page)
        assert result is NextPageResult.STALLED

    async def test_unexpected_exception_propagates(self):
        # Only a genuine Playwright timeout should be converted to STALLED.
        # Anything else (e.g. the TypeError that a wrong wait_for_function
        # call signature would raise) must propagate instead of being
        # silently swallowed and misreported as STALLED.
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=["Viewing 1-25 of 100", "clicked"])
        page.wait_for_function = AsyncMock(side_effect=TypeError("unexpected positional arg"))
        with pytest.raises(TypeError):
            await click_next_page(page)

    async def test_disabled_next_is_last_page(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="disabled")
        page.wait_for_function = AsyncMock()
        result = await click_next_page(page)
        assert result is NextPageResult.LAST_PAGE
