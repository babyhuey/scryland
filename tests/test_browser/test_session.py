"""Tests for browser session management."""

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
