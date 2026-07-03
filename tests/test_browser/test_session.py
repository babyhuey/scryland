"""Tests for browser session management."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scryland.browser.session import BrowserSession
from scryland.config import ScrylandConfig


@pytest.fixture
def config():
    return ScrylandConfig(headless=True, user_data_dir="/tmp/test_scryland_session")


class TestBrowserSession:
    def test_page_raises_before_start(self, config):
        session = BrowserSession(config)
        with pytest.raises(RuntimeError, match="not started"):
            _ = session.page

    @pytest.mark.asyncio
    async def test_close_is_safe_when_not_started(self, config):
        session = BrowserSession(config)
        await session.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_close_stops_playwright_even_if_context_close_raises(self, config):
        # Regression test: a raised context.close() used to skip
        # playwright.stop() entirely, leaking the driver process.
        session = BrowserSession(config)
        mock_context = MagicMock()
        mock_context.storage_state = AsyncMock(return_value={})
        mock_context.close = AsyncMock(side_effect=Exception("close failed"))
        mock_playwright = MagicMock()
        mock_playwright.stop = AsyncMock()

        session._context = mock_context
        session._page = MagicMock()
        session._playwright = mock_playwright

        await session.close()  # Should not raise

        mock_context.close.assert_awaited_once()
        mock_playwright.stop.assert_awaited_once()
        assert session._playwright is None
        assert session._context is None
