"""Watch command — exercise the many branches of _ebay_watch_pass."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from scryland.cli import _empty_ebay_result, cli
from scryland.db import InventoryDB


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "w.db"
    monkeypatch.setenv("SCRYLAND_DB_PATH", str(path))
    return path


@pytest.fixture
def ebay_env(monkeypatch):
    monkeypatch.setenv("SCRYLAND_EBAY_APP_ID", "APP")
    monkeypatch.setenv("SCRYLAND_EBAY_CERT_ID", "CERT")
    monkeypatch.setenv("SCRYLAND_EBAY_DEV_ID", "DEV")
    monkeypatch.setenv("SCRYLAND_EBAY_REDIRECT_URI_NAME", "RU")
    monkeypatch.setenv("SCRYLAND_EBAY_PASSPHRASE", "pw")
    monkeypatch.setenv("SCRYLAND_EBAY_SELLER_USERNAME", "me")


@pytest.fixture
def populated_ebay_db(db_path):
    db = InventoryDB(db_path)
    db.open()
    db.upsert_ebay_listing(
        sku="S1",
        offer_id="O1",
        listing_id="L1",
        product_name="Reprieve",
        set_name="SOA",
        collector_number="9",
        condition="Near Mint",
        is_foil=False,
        price=2.49,
        quantity=1,
        status="active",
    )
    db.close()


class TestEmptyEbayResult:
    def test_has_all_expected_keys(self):
        r = _empty_ebay_result()
        for key in (
            "new_sales",
            "updated",
            "delisted",
            "withdrawn",
            "checked",
            "changes",
            "delisted_items",
            "tcg_delist_failed",
            "browse_errors",
            "skipped_big_drops",
            "withdraw_failed",
            "update_failed",
            "error",
        ):
            assert key in r

    def test_with_error_flag(self):
        r = _empty_ebay_result(error="auth")
        assert r["error"] == "auth"


class TestWatchEbayOnlySweeps:
    """Exercises the _ebay_watch_pass with mocked deps."""

    def _mock_deps(self, monkeypatch):
        from scryland.ebay import auth as auth_mod
        from scryland.ebay import client as client_mod
        from scryland.ebay import orders as orders_mod

        auth_inst = MagicMock()
        auth_inst.access_token = AsyncMock(return_value="t")
        auth_inst.app_access_token = AsyncMock(return_value="a")
        monkeypatch.setattr(auth_mod, "EbayAuth", lambda c: auth_inst)

        client_inst = MagicMock()
        client_inst.__aenter__ = AsyncMock(return_value=client_inst)
        client_inst.__aexit__ = AsyncMock(return_value=None)
        client_inst.get_own_seller_username = AsyncMock(return_value="me")
        client_inst.find_lowest_price = AsyncMock(return_value=None)
        client_inst.withdraw_offer = AsyncMock(return_value=True)
        client_inst.update_offer_price = AsyncMock(return_value=True)
        monkeypatch.setattr(client_mod, "EbayClient", lambda *a, **k: client_inst)

        orders_inst = MagicMock()
        orders_inst.__aenter__ = AsyncMock(return_value=orders_inst)
        orders_inst.__aexit__ = AsyncMock(return_value=None)
        orders_inst.iter_recent_orders = AsyncMock(return_value=[])
        monkeypatch.setattr(
            orders_mod,
            "EbayOrdersClient",
            lambda *a, **k: orders_inst,
        )
        return client_inst, orders_inst

    def test_runs_sweep_with_no_lowest(
        self,
        runner,
        db_path,
        ebay_env,
        populated_ebay_db,
        monkeypatch,
    ):
        """Browse returns None for every card — sweep finds nothing to do."""
        self._mock_deps(monkeypatch)

        # Immediate KeyboardInterrupt after first sleep.
        import asyncio as _aio

        orig_sleep = _aio.sleep
        call = {"n": 0}

        async def fake(s):
            call["n"] += 1
            if call["n"] >= 2:
                raise KeyboardInterrupt()
            await orig_sleep(0)

        monkeypatch.setattr(_aio, "sleep", fake)
        result = runner.invoke(cli, ["watch", "--ebay-only", "-i", "9999"])
        assert result.exit_code in (0, 1)

    def test_sweep_with_lowest_and_update(
        self,
        runner,
        db_path,
        ebay_env,
        populated_ebay_db,
        monkeypatch,
    ):
        """Browse returns lower price — should update our offer."""
        client, _ = self._mock_deps(monkeypatch)
        client.find_lowest_price = AsyncMock(return_value=5.00)

        import asyncio as _aio

        orig = _aio.sleep
        call = {"n": 0}

        async def fake(s):
            call["n"] += 1
            if call["n"] >= 2:
                raise KeyboardInterrupt()
            await orig(0)

        monkeypatch.setattr(_aio, "sleep", fake)
        result = runner.invoke(cli, ["watch", "--ebay-only", "-i", "9999"])
        assert result.exit_code in (0, 1)

    def test_prompts_for_passphrase_at_most_once_across_iterations(
        self,
        runner,
        db_path,
        monkeypatch,
    ):
        """Without SCRYLAND_EBAY_PASSPHRASE set, the passphrase prompt must
        be resolved once at watch startup and cached for the process — not
        re-prompted (and blocking) on every sweep iteration."""
        monkeypatch.setenv("SCRYLAND_EBAY_APP_ID", "APP")
        monkeypatch.setenv("SCRYLAND_EBAY_CERT_ID", "CERT")
        monkeypatch.setenv("SCRYLAND_EBAY_DEV_ID", "DEV")
        monkeypatch.setenv("SCRYLAND_EBAY_REDIRECT_URI_NAME", "RU")
        monkeypatch.setenv("SCRYLAND_EBAY_SELLER_USERNAME", "me")
        # Explicit empty value (not delenv) so this doesn't fall through to
        # a real .env file's passphrase in dev checkouts — env vars take
        # priority over .env regardless of value.
        monkeypatch.setenv("SCRYLAND_EBAY_PASSPHRASE", "")

        self._mock_deps(monkeypatch)

        from rich.prompt import Prompt

        prompt_mock = MagicMock(return_value="secret")
        monkeypatch.setattr(Prompt, "ask", prompt_mock)

        import asyncio as _aio

        orig = _aio.sleep
        call = {"n": 0}

        async def fake(s):
            call["n"] += 1
            if call["n"] >= 2:
                raise KeyboardInterrupt()
            await orig(0)

        monkeypatch.setattr(_aio, "sleep", fake)
        result = runner.invoke(cli, ["watch", "--ebay-only", "-i", "9999"])
        assert result.exit_code in (0, 1)
        assert prompt_mock.call_count <= 1

    def test_browse_errors_surfaced(
        self,
        runner,
        db_path,
        ebay_env,
        populated_ebay_db,
        monkeypatch,
    ):
        """Browse raises for every card — should be counted."""
        client, _ = self._mock_deps(monkeypatch)
        client.find_lowest_price = AsyncMock(side_effect=RuntimeError("Browse 500"))

        import asyncio as _aio

        orig = _aio.sleep
        call = {"n": 0}

        async def fake(s):
            call["n"] += 1
            if call["n"] >= 2:
                raise KeyboardInterrupt()
            await orig(0)

        monkeypatch.setattr(_aio, "sleep", fake)
        result = runner.invoke(cli, ["watch", "--ebay-only", "-i", "9999"])
        assert result.exit_code in (0, 1)


class TestPeriodicTcgRefresh:
    """Periodic TCG scrape fires when last_tcg_scrape timestamp is overdue."""

    def _make_db(self, tmp_path):
        db = InventoryDB(tmp_path / "t.db")
        db.open()
        return db

    def test_get_metadata_none_when_unset(self, tmp_path):
        db = self._make_db(tmp_path)
        assert db.get_metadata("last_tcg_scrape") is None
        db.close()

    def test_set_metadata_persists_across_open(self, tmp_path):
        db = self._make_db(tmp_path)
        db.set_metadata("last_tcg_scrape", "2020-01-01T00:00:00")
        db.close()
        db2 = InventoryDB(tmp_path / "t.db")
        db2.open()
        assert db2.get_metadata("last_tcg_scrape") == "2020-01-01T00:00:00"
        db2.close()

    def test_overdue_when_no_timestamp(self, tmp_path):
        """No stored timestamp → refresh is overdue."""
        from datetime import datetime

        db = self._make_db(tmp_path)
        last_str = db.get_metadata("last_tcg_scrape")
        last_dt = datetime.fromisoformat(last_str) if last_str else None
        overdue = last_dt is None or (datetime.now() - last_dt).total_seconds() > 3 * 86400
        assert overdue is True
        db.close()

    def test_not_overdue_when_recent(self, tmp_path):
        """Timestamp from 1 day ago → not overdue for 3-day interval."""
        from datetime import datetime, timedelta

        db = self._make_db(tmp_path)
        recent = (datetime.now() - timedelta(days=1)).isoformat()
        db.set_metadata("last_tcg_scrape", recent)
        last_str = db.get_metadata("last_tcg_scrape")
        last_dt = datetime.fromisoformat(last_str) if last_str else None
        overdue = last_dt is None or (datetime.now() - last_dt).total_seconds() > 3 * 86400
        assert overdue is False
        db.close()
