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


class TestExpiresInDefault:
    """Missing expires_in must not be treated as an instantly-expired (0s)
    token — that forces a refetch on every single call."""

    def test_missing_expires_in_defaults_conservatively(self, config_prod, caplog):
        import logging

        auth = EbayAuth(config_prod)
        with caplog.at_level(logging.WARNING, logger="scryland"):
            bundle = auth._parse_token_response({"access_token": "tok"})
        assert bundle.expires_at > time.time() + 1000
        assert any("expires_in" in r.message for r in caplog.records)

    def test_present_expires_in_respected(self, config_prod):
        auth = EbayAuth(config_prod)
        bundle = auth._parse_token_response({"access_token": "tok", "expires_in": 7200})
        assert bundle.expires_at == pytest.approx(time.time() + 7200, abs=2)

    async def test_app_token_missing_expires_in_defaults_conservatively(
        self, config_prod, monkeypatch, caplog
    ):
        import logging

        import httpx as _httpx

        from scryland.ebay import auth as auth_mod

        def handler(req):
            return _httpx.Response(200, json={"access_token": "app-tok"})

        _mock_httpx_client(monkeypatch, auth_mod, handler)
        auth = EbayAuth(config_prod)
        with caplog.at_level(logging.WARNING, logger="scryland"):
            tok = await auth.app_access_token()
        assert tok == "app-tok"
        assert auth._app_token_expires_at > time.time() + 1000
        assert any("expires_in" in r.message for r in caplog.records)


class TestConcurrentRefresh:
    async def test_exactly_one_refresh_for_concurrent_callers(self, config_prod, monkeypatch):
        """8 concurrent access_token() calls against an expired cached token
        must trigger exactly one _refresh — the rest wait on the lock and
        reuse the freshly-refreshed token."""
        import asyncio

        auth = EbayAuth(config_prod)
        expired = TokenBundle(
            access_token="old",
            expires_at=time.time() - 10,
            refresh_token="r",
        )
        auth._save(expired, "pw")

        refresh_calls = {"n": 0}

        async def fake_refresh(refresh_token):
            refresh_calls["n"] += 1
            await asyncio.sleep(0.01)  # force a real interleaving window
            return TokenBundle(
                access_token="new-tok",
                expires_at=time.time() + 3600,
                refresh_token="r2",
            )

        monkeypatch.setattr(auth, "_refresh", fake_refresh)

        results = await asyncio.gather(*(auth.access_token("pw") for _ in range(8)))

        assert refresh_calls["n"] == 1
        assert all(tok == "new-tok" for tok in results)


class TestAtomicWriteHelper:
    def test_writes_target_with_0600_perms(self, tmp_path):
        from scryland.ebay.auth import _write_secure_atomic

        target = tmp_path / "secret.bin"
        _write_secure_atomic(target, b"hello")
        assert target.read_bytes() == b"hello"
        assert oct(target.stat().st_mode)[-3:] == "600"

    def test_no_leftover_temp_file(self, tmp_path):
        from scryland.ebay.auth import _write_secure_atomic

        target = tmp_path / "secret.bin"
        _write_secure_atomic(target, b"hello")
        leftovers = [p for p in tmp_path.iterdir() if p.name != "secret.bin"]
        assert leftovers == []

    def test_uses_atomic_replace_not_direct_write(self, tmp_path, monkeypatch):
        """A crash between staging the new payload and the final rename must
        never leave `target` holding a half-written file — verify the write
        goes through os.replace rather than a direct write to the final path."""
        from scryland.ebay import auth as auth_mod

        target = tmp_path / "secret.bin"
        target.write_bytes(b"OLD")  # pre-existing file, simulating a prior save

        replace_calls = []
        orig_replace = auth_mod.os.replace

        def spy_replace(src, dst):
            # At the moment of replace, the temp file must already hold the
            # full new payload while the target still has the OLD content —
            # proving the new data was staged, not written in place.
            assert Path(src).read_bytes() == b"NEW-DATA"
            assert target.read_bytes() == b"OLD"
            replace_calls.append((src, dst))
            return orig_replace(src, dst)

        monkeypatch.setattr(auth_mod.os, "replace", spy_replace)
        auth_mod._write_secure_atomic(target, b"NEW-DATA")

        assert len(replace_calls) == 1
        assert target.read_bytes() == b"NEW-DATA"
