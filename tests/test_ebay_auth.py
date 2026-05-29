"""Tests for EbayAuth — OAuth URL construction, refresh-token error mapping."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from scryland.config import ScrylandConfig
from scryland.ebay.auth import EbayAuth, TokenBundle, _endpoints


@pytest.fixture
def config_prod(tmp_path):
    return ScrylandConfig(
        ebay_environment="production",
        ebay_app_id="APP_ID",
        ebay_cert_id="CERT_ID",
        ebay_dev_id="DEV_ID",
        ebay_redirect_uri_name="MyRuName",
        ebay_credentials_path=str(tmp_path / "creds.bin"),
    )


@pytest.fixture
def config_sandbox(tmp_path):
    return ScrylandConfig(
        ebay_environment="sandbox",
        ebay_app_id="APP",
        ebay_cert_id="CERT",
        ebay_dev_id="DEV",
        ebay_redirect_uri_name="RU",
        ebay_credentials_path=str(tmp_path / "creds.bin"),
    )


class TestEndpointRouting:
    def test_production(self):
        auth_url, token_url, api_base = _endpoints("production")
        assert "sandbox" not in auth_url
        assert "sandbox" not in token_url
        assert "sandbox" not in api_base

    def test_sandbox(self):
        auth_url, token_url, api_base = _endpoints("sandbox")
        assert "sandbox" in auth_url
        assert "sandbox" in token_url
        assert "sandbox" in api_base

    def test_unknown_env_defaults_to_production(self):
        _, _, base = _endpoints("potato")
        assert "sandbox" not in base


class TestConsentUrl:
    def test_includes_client_id_and_scopes(self, config_prod):
        auth = EbayAuth(config_prod)
        url = auth.consent_url()
        assert "client_id=APP_ID" in url
        assert "redirect_uri=MyRuName" in url
        assert "scope=" in url
        assert "sell.inventory" in url
        assert "sell.fulfillment" in url  # added for orders/sales

    def test_sandbox_url_uses_sandbox_auth_host(self, config_sandbox):
        auth = EbayAuth(config_sandbox)
        url = auth.consent_url()
        assert url.startswith("https://auth.sandbox.ebay.com")

    def test_missing_app_id_raises(self, tmp_path):
        c = ScrylandConfig(
            ebay_app_id="",
            ebay_redirect_uri_name="RU",
            ebay_credentials_path=str(tmp_path / "x"),
        )
        with pytest.raises(RuntimeError, match="ebay_app_id"):
            EbayAuth(c).consent_url()


class TestSaveLoad:
    def test_roundtrip(self, config_prod):
        auth = EbayAuth(config_prod)
        bundle = TokenBundle(
            access_token="access-xyz",
            expires_at=time.time() + 3600,
            refresh_token="refresh-abc",
        )
        auth._save(bundle, passphrase="hunter2")

        # New instance (simulates reload).
        auth2 = EbayAuth(config_prod)
        loaded = auth2._load("hunter2")
        assert loaded.access_token == "access-xyz"
        assert loaded.refresh_token == "refresh-abc"

    def test_wrong_passphrase_raises(self, config_prod):
        auth = EbayAuth(config_prod)
        bundle = TokenBundle(
            access_token="a",
            expires_at=time.time() + 3600,
            refresh_token="r",
        )
        auth._save(bundle, passphrase="right")

        auth2 = EbayAuth(config_prod)
        with pytest.raises(RuntimeError, match="Wrong passphrase"):
            auth2._load("wrong")

    def test_missing_credentials_file_raises(self, config_prod):
        auth = EbayAuth(config_prod)
        with pytest.raises(RuntimeError, match="ebay-auth"):
            auth._load("anything")


class TestAccessTokenCache:
    async def test_cached_token_not_refetched(self, config_prod):
        auth = EbayAuth(config_prod)
        auth._cached = TokenBundle(
            access_token="cached-tok",
            expires_at=time.time() + 3600,  # 1h left
            refresh_token="r",
        )
        tok = await auth.access_token("pw")
        assert tok == "cached-tok"


def _mock_httpx_client(monkeypatch, auth_mod, handler):
    """Patch httpx.AsyncClient inside the auth module to use a MockTransport."""
    import httpx as _httpx

    orig = _httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = _httpx.MockTransport(handler)
        return orig(**kwargs)

    monkeypatch.setattr(auth_mod.httpx, "AsyncClient", factory)


class TestRefreshErrorMapping:
    """The _refresh method should convert invalid_grant into a clear
    "run ebay-auth again" message."""

    async def test_invalid_grant_raises_actionable(self, config_prod, monkeypatch):
        import httpx as _httpx

        from scryland.ebay import auth as auth_mod

        def handler(req):
            return _httpx.Response(400, json={"error": "invalid_grant"})

        _mock_httpx_client(monkeypatch, auth_mod, handler)
        auth = EbayAuth(config_prod)
        with pytest.raises(RuntimeError, match="ebay-auth"):
            await auth._refresh("stale-refresh")

    async def test_other_4xx_propagates(self, config_prod, monkeypatch):
        import httpx as _httpx

        from scryland.ebay import auth as auth_mod

        def handler(req):
            return _httpx.Response(500, text="server err")

        _mock_httpx_client(monkeypatch, auth_mod, handler)
        auth = EbayAuth(config_prod)
        with pytest.raises(_httpx.HTTPStatusError):
            await auth._refresh("token")


class TestExchangeCode:
    async def test_happy_path(self, config_prod, monkeypatch):
        import httpx as _httpx

        from scryland.ebay import auth as auth_mod

        captured = {}

        def handler(req):
            captured["data"] = req.content
            return _httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 7200,
                },
            )

        _mock_httpx_client(monkeypatch, auth_mod, handler)
        auth = EbayAuth(config_prod)
        bundle = await auth.exchange_code("the_code", "pw")
        assert bundle.access_token == "at"
        assert bundle.refresh_token == "rt"
        assert b"code=the_code" in captured["data"]
        # It should have persisted the tokens.
        from pathlib import Path

        assert Path(config_prod.ebay_credentials_path).exists()


class TestAppAccessToken:
    async def test_client_credentials_grant(self, config_prod, monkeypatch):
        import httpx as _httpx

        from scryland.ebay import auth as auth_mod

        call_count = {"n": 0}

        def handler(req):
            call_count["n"] += 1
            return _httpx.Response(
                200,
                json={
                    "access_token": "app-tok",
                    "expires_in": 7200,
                },
            )

        _mock_httpx_client(monkeypatch, auth_mod, handler)
        auth = EbayAuth(config_prod)
        tok = await auth.app_access_token()
        assert tok == "app-tok"
        # Second call uses cache — no new network.
        tok2 = await auth.app_access_token()
        assert tok2 == "app-tok"
        assert call_count["n"] == 1


class TestRequireConfig:
    def test_missing_field_message(self, tmp_path):
        c = ScrylandConfig(
            ebay_app_id="",  # missing!
            ebay_cert_id="cert",
            ebay_redirect_uri_name="ru",
            ebay_credentials_path=str(tmp_path / "x"),
        )
        auth = EbayAuth(c)
        with pytest.raises(RuntimeError, match="ebay_app_id"):
            auth._require("ebay_app_id", "ebay_cert_id")


class TestRandomSalt:
    def test_save_creates_salt_file(self, config_prod):
        """_save must create a separate salt file."""
        auth = EbayAuth(config_prod)
        bundle = TokenBundle(access_token="tok", expires_at=9999999999.0, refresh_token="ref")
        auth._save(bundle, "passphrase")
        salt_path = Path(str(config_prod.ebay_credentials_path) + ".salt")
        assert salt_path.exists()
        assert len(salt_path.read_bytes()) == 16

    def test_two_saves_use_different_salts(self, config_prod):
        """Each save must generate a fresh random salt."""
        auth = EbayAuth(config_prod)
        bundle = TokenBundle(access_token="tok", expires_at=9999999999.0, refresh_token="ref")
        auth._save(bundle, "passphrase")
        salt_path = Path(str(config_prod.ebay_credentials_path) + ".salt")
        salt1 = salt_path.read_bytes()
        auth._save(bundle, "passphrase")
        salt2 = salt_path.read_bytes()
        assert salt1 != salt2

    def test_roundtrip_with_random_salt(self, config_prod):
        """Credentials saved with random salt must be loadable."""
        auth = EbayAuth(config_prod)
        bundle = TokenBundle(access_token="acc", expires_at=9999999999.0, refresh_token="ref")
        auth._save(bundle, "mypassphrase")
        loaded = auth._load("mypassphrase")
        assert loaded.access_token == "acc"
        assert loaded.refresh_token == "ref"

    def test_migration_from_hardcoded_salt(self, config_prod):
        """Old credentials (no salt file) must still load via hardcoded-salt fallback."""
        import json

        from cryptography.fernet import Fernet

        from scryland.credentials import _derive_key

        # Manually create credentials with the OLD hardcoded salt
        old_salt = b"scryland-ebay-salt"
        key = _derive_key("oldpassphrase", old_salt)
        payload = json.dumps(
            {
                "environment": "production",
                "access_token": "oldtok",
                "expires_at": 9999999999.0,
                "refresh_token": "oldref",
            }
        ).encode()
        encrypted = Fernet(key).encrypt(payload)
        Path(config_prod.ebay_credentials_path).write_bytes(encrypted)
        # Do NOT create the .salt file (simulates old install)

        auth = EbayAuth(config_prod)
        loaded = auth._load("oldpassphrase")
        assert loaded.access_token == "oldtok"


class TestParseTokenResponseGuard:
    def test_missing_access_token_raises_runtime_error(self, config_prod):
        """A 200 response without access_token must raise RuntimeError, not KeyError."""
        auth = EbayAuth(config_prod)
        with pytest.raises(RuntimeError, match="access_token"):
            auth._parse_token_response({})

    def test_valid_response_parsed_correctly(self, config_prod):
        auth = EbayAuth(config_prod)
        bundle = auth._parse_token_response(
            {"access_token": "tok", "expires_in": 7200, "refresh_token": "ref"}
        )
        assert bundle.access_token == "tok"
        assert bundle.refresh_token == "ref"
