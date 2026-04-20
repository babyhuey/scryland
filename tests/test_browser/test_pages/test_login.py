"""Tests for login page detection."""

from unittest.mock import AsyncMock

import pytest

from scryland.browser.pages.login import LoginPage, _is_admin_page, _is_login_page
from scryland.config import ScrylandConfig


@pytest.fixture
def config():
    return ScrylandConfig(headless=True)


class TestIsAdminPage:
    def test_admin_catalog(self):
        assert _is_admin_page("https://store.tcgplayer.com/admin/product/catalog") is True

    def test_admin_pricing(self):
        assert _is_admin_page("https://store.tcgplayer.com/admin/pricing") is True

    def test_seller_portal(self):
        assert _is_admin_page("https://sellerportal.tcgplayer.com/inventory") is True

    def test_login_page_with_admin_return_url(self):
        # The key fix: /oauth/login path should NOT match even if returnUrl has /admin
        url = "https://store.tcgplayer.com/oauth/login?returnUrl=/admin/product/catalog"
        assert _is_admin_page(url) is False

    def test_random_url(self):
        assert _is_admin_page("https://example.com") is False


class TestIsLoginPage:
    def test_oauth_login(self):
        assert _is_login_page("https://store.tcgplayer.com/oauth/login?returnUrl=...") is True

    def test_account_login(self):
        assert _is_login_page("https://store.tcgplayer.com/account/login") is True

    def test_accounts_domain(self):
        assert _is_login_page("https://accounts.tcgplayer.com/something") is True

    def test_admin_page_not_login(self):
        assert _is_login_page("https://store.tcgplayer.com/admin/product/catalog") is False


class TestLoginDetection:
    @pytest.mark.asyncio
    async def test_detects_oauth_login_url(self, config):
        page = AsyncMock()
        page.url = "https://store.tcgplayer.com/oauth/login?returnUrl=/admin/account/logon"
        login_page = LoginPage(page, config)
        assert await login_page.is_login_required() is True

    @pytest.mark.asyncio
    async def test_not_required_when_on_admin(self, config):
        page = AsyncMock()
        page.url = "https://store.tcgplayer.com/admin/product/catalog"
        login_page = LoginPage(page, config)
        assert await login_page.is_login_required() is False

    @pytest.mark.asyncio
    async def test_not_required_on_seller_portal(self, config):
        page = AsyncMock()
        page.url = "https://sellerportal.tcgplayer.com/inventory"
        login_page = LoginPage(page, config)
        assert await login_page.is_login_required() is False

    @pytest.mark.asyncio
    async def test_required_when_on_unknown_url(self, config):
        page = AsyncMock()
        page.url = "https://example.com/something"
        login_page = LoginPage(page, config)
        assert await login_page.is_login_required() is True


class TestVerifyLoggedIn:
    @pytest.mark.asyncio
    async def test_verified_on_admin(self, config):
        page = AsyncMock()
        page.url = "https://store.tcgplayer.com/admin/product/catalog"
        login_page = LoginPage(page, config)
        assert await login_page.verify_logged_in() is True

    @pytest.mark.asyncio
    async def test_not_verified_on_login_page(self, config):
        page = AsyncMock()
        page.url = "https://store.tcgplayer.com/oauth/login?returnUrl=/admin/foo"
        login_page = LoginPage(page, config)
        assert await login_page.verify_logged_in() is False
