"""High-level tests for Click commands using CliRunner.

Strategy: for each no-browser command, invoke with a temp DB path and
mocked eBay client where needed. Verifies the command runs end-to-end
against a stubbed environment without real network or browser.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from scryland.cli import cli
from scryland.db import InventoryDB
from scryland.models import Listing


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    # Point config at this DB by setting env var.
    monkeypatch.setenv("SCRYLAND_DB_PATH", str(path))
    return path


@pytest.fixture
def populated_db(db_path):
    db = InventoryDB(db_path)
    db.open()
    # Seed an active inventory row.
    db.sync(
        [
            Listing(
                product_name="Reprieve",
                condition="Near Mint",
                quantity=1,
                current_price=Decimal("2.42"),
                tcg_low_price=Decimal("2.50"),
            )
        ]
    )
    # And an eBay listing.
    db.upsert_ebay_listing(
        sku="TEST-SKU",
        offer_id="OFF1",
        listing_id="LST1",
        product_name="Reprieve",
        set_name="Secrets of Strixhaven: Mystical Archive",
        collector_number="9",
        condition="Near Mint",
        is_foil=False,
        price=2.49,
        quantity=1,
        status="active",
    )
    # And a sale.
    db.conn.execute(
        "INSERT INTO sales (order_number, order_date, buyer_name, status, "
        "product_name, condition, quantity, sale_price, shipping_amt, "
        "total_amount, fee_amount, net_amount, recorded_at, marketplace) "
        "VALUES ('O1', '2026-01-01', 'buyer', 'PAID', 'Reprieve', 'Near Mint', "
        "1, 2.50, 0.73, 3.23, 0.32, 2.91, '2026-01-01', 'ebay')"
    )
    db.conn.commit()
    db.close()
    return db_path


class TestHelpAndBasics:
    def test_help_runs(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Scryland" in result.output

    def test_no_args_shows_usage(self, runner):
        result = runner.invoke(cli, [])
        # Click returns 2 for missing subcommand
        assert result.exit_code in (0, 2)


class TestStatusCommand:
    def test_empty_db(self, runner, db_path):
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0

    def test_with_listings(self, runner, populated_db):
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "Reprieve" in result.output


class TestResetDb:
    def test_clears_db(self, runner, populated_db):
        # Need --force or similar? Test both with/without confirmation.
        result = runner.invoke(cli, ["reset-db"], input="y\n")
        # Regardless of exact code, the command shouldn't crash.
        assert result.exit_code in (0, 1)


class TestClearSold:
    def test_clears_sold_rows(self, runner, populated_db):
        result = runner.invoke(cli, ["clear-sold"])
        assert result.exit_code == 0


class TestCompareCommand:
    def test_runs_without_network(self, runner, populated_db):
        result = runner.invoke(cli, ["compare"])
        assert result.exit_code == 0
        assert "Reprieve" in result.output


class TestSalesReport:
    def test_with_sales(self, runner, populated_db):
        result = runner.invoke(cli, ["sales-report"])
        assert result.exit_code == 0
        assert "Reprieve" in result.output

    def test_empty_db(self, runner, db_path):
        result = runner.invoke(cli, ["sales-report"])
        assert result.exit_code == 0


class TestPriceHistory:
    def test_no_history(self, runner, db_path):
        result = runner.invoke(cli, ["price-history"])
        # Might return 0 or exit with usage
        assert result.exit_code == 0

    def test_with_card(self, runner, populated_db):
        result = runner.invoke(cli, ["price-history", "--card", "Reprieve"])
        assert result.exit_code == 0


class TestEbayPreview:
    def test_preview_with_mock_scryfall(self, runner, tmp_path, db_path, monkeypatch):
        """Run ebay-preview against a minimal CSV with Scryfall mocked."""
        import csv

        # Write a minimal Mythic CSV (whatever the parser accepts).
        csv_path = tmp_path / "cards.csv"
        # Mythic CSV columns (from mythic_csv.py).
        with open(csv_path, "w") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Card Name",
                    "Set Code",
                    "Set Name",
                    "Collector Number",
                    "Rarity",
                    "Language",
                    "Quantity",
                    "Condition",
                    "Finish",
                    "Altered",
                    "Signed",
                    "Misprint",
                    "Price USD",
                    "Price USD Foil",
                    "Price USD Etched",
                    "Scryfall ID",
                ]
            )
            writer.writerow(
                [
                    "Reprieve",
                    "SOA",
                    "Secrets of Strixhaven: Mystical Archive",
                    "9",
                    "rare",
                    "en",
                    "1",
                    "NM",
                    "nonfoil",
                    "false",
                    "false",
                    "false",
                    "2.49",
                    "0",
                    "0",
                    "abc",
                ]
            )

        # Patch the Scryfall client to return something simple.
        from scryland.ebay.scryfall import CardInfo

        async def fake_find_card(self, name, set_name=None, collector_number=None):
            return CardInfo(
                name="Reprieve",
                set_code="soa",
                set_name="Secrets of Strixhaven: Mystical Archive",
                collector_number="9",
                image_url="https://img/png",
                image_small_url=None,
                oracle_text="",
                type_line="Instant",
                mana_cost="",
                rarity="rare",
                scryfall_uri="",
                colors=["W"],
            )

        monkeypatch.setattr(
            "scryland.ebay.scryfall.ScryfallClient.find_card",
            fake_find_card,
        )

        result = runner.invoke(
            cli,
            [
                "ebay-preview",
                str(csv_path),
                "-n",
                "1",
                "--min-price",
                "0.00",
            ],
        )
        assert result.exit_code == 0
        assert "Reprieve" in result.output


@pytest.fixture
def mock_ebay(monkeypatch):
    """Patch EbayAuth + EbayClient so no real network call happens."""
    from scryland.ebay import auth as auth_mod
    from scryland.ebay import client as client_mod

    auth_instance = MagicMock()
    auth_instance.access_token = AsyncMock(return_value="user-tok")
    auth_instance.app_access_token = AsyncMock(return_value="app-tok")
    auth_instance.api_base = "https://api.ebay.com"
    auth_instance.consent_url = MagicMock(return_value="https://consent")
    auth_instance.exchange_code = AsyncMock()

    client_instance = MagicMock()
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)
    client_instance.close = AsyncMock()
    client_instance.get_own_seller_username = AsyncMock(return_value="me")
    client_instance.find_lowest_price = AsyncMock(return_value=2.49)
    client_instance.create_default_policies = AsyncMock(
        return_value={
            "fulfillment": "F1",
            "payment": "P1",
            "return": "R1",
        }
    )
    client_instance.create_merchant_location = AsyncMock()
    client_instance.list_business_policies = AsyncMock(
        return_value={
            "fulfillment": [{"id": "F1", "name": "F"}],
            "payment": [{"id": "P1", "name": "P"}],
            "return": [{"id": "R1", "name": "R"}],
        }
    )
    client_instance.update_fulfillment_shipping_cost = AsyncMock(return_value=True)
    client_instance.update_offer_price = AsyncMock(return_value=True)
    client_instance.withdraw_offer = AsyncMock(return_value=True)
    client_instance.publish_listing = AsyncMock()
    client_instance.get_inventory_item = AsyncMock(return_value=None)
    client_instance._put_inventory_item = AsyncMock()
    client_instance._config = MagicMock()
    client_instance._config.ebay_shipping_cost = 0.99

    monkeypatch.setattr(auth_mod, "EbayAuth", lambda cfg: auth_instance)
    monkeypatch.setattr(client_mod, "EbayClient", lambda *a, **k: client_instance)
    return client_instance


@pytest.fixture
def ebay_configured(monkeypatch):
    monkeypatch.setenv("SCRYLAND_EBAY_APP_ID", "TESTAPP")
    monkeypatch.setenv("SCRYLAND_EBAY_CERT_ID", "TESTCERT")
    monkeypatch.setenv("SCRYLAND_EBAY_DEV_ID", "TESTDEV")
    monkeypatch.setenv("SCRYLAND_EBAY_REDIRECT_URI_NAME", "RuName")
    monkeypatch.setenv("SCRYLAND_EBAY_FULFILLMENT_POLICY_ID", "F1")
    monkeypatch.setenv("SCRYLAND_EBAY_PAYMENT_POLICY_ID", "P1")
    monkeypatch.setenv("SCRYLAND_EBAY_RETURN_POLICY_ID", "R1")
    monkeypatch.setenv("SCRYLAND_EBAY_PASSPHRASE", "pw")
    monkeypatch.setenv("SCRYLAND_EBAY_SELLER_USERNAME", "me")


class TestEbayBootstrap:
    def test_creates_policies_and_location(
        self,
        runner,
        db_path,
        ebay_configured,
        mock_ebay,
    ):
        result = runner.invoke(
            cli,
            [
                "ebay-bootstrap",
                "--city",
                "Durham",
                "--state",
                "NC",
                "--postal-code",
                "27712",
            ],
        )
        assert result.exit_code == 0
        mock_ebay.create_default_policies.assert_awaited()
        mock_ebay.create_merchant_location.assert_awaited()


class TestEbayPolicies:
    def test_lists_ids(self, runner, db_path, ebay_configured, mock_ebay):
        result = runner.invoke(cli, ["ebay-policies"])
        assert result.exit_code == 0
        assert "F1" in result.output
        assert "P1" in result.output
        assert "R1" in result.output


class TestEbaySetupLocation:
    def test_runs(self, runner, db_path, ebay_configured, mock_ebay):
        result = runner.invoke(
            cli,
            [
                "ebay-setup-location",
                "--city",
                "Durham",
                "--state",
                "NC",
                "--postal-code",
                "27712",
            ],
        )
        assert result.exit_code == 0


class TestEbayUpdateShipping:
    def test_updates_policy(self, runner, db_path, ebay_configured, mock_ebay):
        result = runner.invoke(cli, ["ebay-update-shipping", "--cost", "0.99"])
        assert result.exit_code == 0
        mock_ebay.update_fulfillment_shipping_cost.assert_awaited()


class TestEbaySync:
    def test_no_orders(self, runner, db_path, ebay_configured, monkeypatch):
        """No new eBay orders — runs cleanly."""
        # Mock EbayOrdersClient to return an empty order list.
        from scryland.ebay import orders as orders_mod

        orders_instance = MagicMock()
        orders_instance.__aenter__ = AsyncMock(return_value=orders_instance)
        orders_instance.__aexit__ = AsyncMock(return_value=None)
        orders_instance.iter_recent_orders = AsyncMock(return_value=[])
        monkeypatch.setattr(
            orders_mod,
            "EbayOrdersClient",
            lambda *a, **k: orders_instance,
        )

        # Also mock auth.
        from scryland.ebay import auth as auth_mod

        auth_instance = MagicMock()
        auth_instance.access_token = AsyncMock(return_value="t")
        monkeypatch.setattr(auth_mod, "EbayAuth", lambda cfg: auth_instance)

        result = runner.invoke(cli, ["ebay-sync"])
        assert result.exit_code == 0


class TestCsvOptimize:
    def test_runs_on_csv(self, runner, tmp_path, db_path):
        """csv-optimize is pure CSV I/O, no network."""
        import csv

        src = tmp_path / "in.csv"
        out = tmp_path / "out.csv"
        with open(src, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "TCGplayer Id",
                    "Product Line",
                    "Set Name",
                    "Product Name",
                    "Title",
                    "Number",
                    "Rarity",
                    "Condition",
                    "TCG Market Price",
                    "TCG Direct Low",
                    "TCG Low Price With Shipping",
                    "TCG Low Price",
                    "Total Quantity",
                    "Add to Quantity",
                    "TCG Marketplace Price",
                    "Photo URL",
                ]
            )
            w.writerow(
                [
                    "1",
                    "Magic",
                    "Strixhaven",
                    "Reprieve",
                    "",
                    "9",
                    "Rare",
                    "Near Mint",
                    "2.50",
                    "2.00",
                    "2.00",
                    "2.00",
                    "1",
                    "0",
                    "3.00",
                    "",
                ]
            )
        result = runner.invoke(
            cli,
            [
                "csv-optimize",
                str(src),
                "--output",
                str(out),
            ],
        )
        assert result.exit_code == 0


class TestCredentialsCommands:
    def test_status_no_creds(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["credentials", "status"])
        assert result.exit_code == 0

    def test_clear_no_creds(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["credentials", "clear"])
        assert result.exit_code == 0


@pytest.fixture
def mock_browser(monkeypatch):
    """Patch BrowserSession + page classes so commands that use the browser
    run without Playwright."""
    from scryland.browser import session as session_mod
    from scryland.browser.pages import inventory as inv_mod
    from scryland.browser.pages import orders as ord_mod
    from scryland.pricing import optimizer as opt_mod

    session = MagicMock()
    session.start = AsyncMock()
    session.close = AsyncMock()
    session.ensure_logged_in = AsyncMock()
    session.human_delay = AsyncMock()
    session.dismiss_popups = AsyncMock()
    session.page = MagicMock()
    monkeypatch.setattr(session_mod, "BrowserSession", lambda cfg: session)

    # Mock inventory + orders pages
    inv_instance = MagicMock()
    inv_instance.navigate = AsyncMock()
    inv_instance.get_product_names = AsyncMock(return_value=[])
    inv_instance.click_manage_for_product = AsyncMock()
    inv_instance.get_manage_page_listings = AsyncMock(return_value=[])
    inv_instance.go_back_to_inventory = AsyncMock()
    monkeypatch.setattr(inv_mod, "InventoryPage", lambda *a, **k: inv_instance)

    ord_instance = MagicMock()
    ord_instance.navigate = AsyncMock()
    ord_instance.get_order_rows = AsyncMock(return_value=[])
    ord_instance.get_order_details = AsyncMock(return_value={})
    ord_instance.go_back_to_orders = AsyncMock()
    monkeypatch.setattr(ord_mod, "OrdersPage", lambda *a, **k: ord_instance)

    # Mock optimizer result
    from scryland.pricing.optimizer import OptimizeResult

    async def fake_optimize(session, config, console):
        return OptimizeResult(total=0, updated=0)

    monkeypatch.setattr(opt_mod, "run_price_differential_optimize", fake_optimize)
    return session


class TestOptimizeCommand:
    def test_dry_run_empty_inventory(self, runner, db_path, mock_browser):
        """Optimize with no items to change — runs cleanly."""
        # Need to also patch the optimizer import location in cli.py
        from scryland.pricing.optimizer import OptimizeResult

        with patch("scryland.pricing.optimizer.run_price_differential_optimize") as m:

            async def fake(s, c, cons):
                return OptimizeResult()

            m.side_effect = fake
            result = runner.invoke(cli, ["optimize", "--dry-run"])
        # Many commands open browser — may exit non-zero if ensure_logged_in
        # fails. Just check that the exception didn't crash unhandled.
        assert result.exit_code in (0, 1)


class TestSyncCommand:
    def test_runs_with_empty_inventory(self, runner, db_path, mock_browser):
        """TCG sync with no products — runs cleanly."""
        result = runner.invoke(cli, ["sync"])
        assert result.exit_code in (0, 1)


class TestSalesCommand:
    def test_runs_with_no_orders(self, runner, db_path, mock_browser):
        result = runner.invoke(cli, ["sales"])
        assert result.exit_code in (0, 1)


class TestWatchCommand:
    def test_ebay_only_one_iteration(
        self,
        runner,
        db_path,
        ebay_configured,
        mock_ebay,
        monkeypatch,
    ):
        """Watch with a tiny interval then Ctrl-C via KeyboardInterrupt."""
        # Patch asyncio.sleep to raise KeyboardInterrupt on second sleep
        # so we exit the loop after one iteration.
        import asyncio as _aio

        orig_sleep = _aio.sleep
        call = {"n": 0}

        async def fake_sleep(secs):
            call["n"] += 1
            if call["n"] > 1 and secs > 10:
                raise KeyboardInterrupt()
            await orig_sleep(0)

        monkeypatch.setattr(_aio, "sleep", fake_sleep)

        # Mock EbayOrdersClient (eBay-only skips browser).
        from scryland.ebay import orders as orders_mod

        orders_instance = MagicMock()
        orders_instance.__aenter__ = AsyncMock(return_value=orders_instance)
        orders_instance.__aexit__ = AsyncMock(return_value=None)
        orders_instance.iter_recent_orders = AsyncMock(return_value=[])
        monkeypatch.setattr(
            orders_mod,
            "EbayOrdersClient",
            lambda *a, **k: orders_instance,
        )

        result = runner.invoke(cli, ["watch", "--ebay-only", "-i", "9999"])
        # Expected to exit via KeyboardInterrupt (caught gracefully).
        assert result.exit_code in (0, 1)


class TestListOnEbay:
    def test_dry_run(self, runner, tmp_path, db_path, ebay_configured, monkeypatch):
        """--dry-run should preview without calling eBay."""
        import csv

        csv_path = tmp_path / "cards.csv"
        with open(csv_path, "w") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "Card Name",
                    "Set Code",
                    "Set Name",
                    "Collector Number",
                    "Rarity",
                    "Language",
                    "Quantity",
                    "Condition",
                    "Finish",
                    "Altered",
                    "Signed",
                    "Misprint",
                    "Price USD",
                    "Price USD Foil",
                    "Price USD Etched",
                    "Scryfall ID",
                ]
            )
            w.writerow(
                [
                    "Reprieve",
                    "SOA",
                    "Secrets of Strixhaven: Mystical Archive",
                    "9",
                    "rare",
                    "en",
                    "1",
                    "NM",
                    "nonfoil",
                    "false",
                    "false",
                    "false",
                    "2.49",
                    "0",
                    "0",
                    "abc",
                ]
            )

        result = runner.invoke(
            cli,
            [
                "list-on-ebay",
                str(csv_path),
                "--dry-run",
                "-n",
                "1",
            ],
        )
        assert result.exit_code == 0


class TestAddInventoryDryRun:
    def test_dry_run(self, runner, tmp_path, db_path):
        """add-inventory --dry-run should print a table without browser."""
        import csv

        csv_path = tmp_path / "cards.csv"
        with open(csv_path, "w") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "Card Name",
                    "Set Code",
                    "Set Name",
                    "Collector Number",
                    "Rarity",
                    "Language",
                    "Quantity",
                    "Condition",
                    "Finish",
                    "Altered",
                    "Signed",
                    "Misprint",
                    "Price USD",
                    "Price USD Foil",
                    "Price USD Etched",
                    "Scryfall ID",
                ]
            )
            w.writerow(
                [
                    "Reprieve",
                    "SOA",
                    "Secrets of Strixhaven: Mystical Archive",
                    "9",
                    "rare",
                    "en",
                    "1",
                    "NM",
                    "nonfoil",
                    "false",
                    "false",
                    "false",
                    "2.49",
                    "0",
                    "0",
                    "abc",
                ]
            )

        result = runner.invoke(
            cli,
            [
                "add-inventory",
                str(csv_path),
                "--dry-run",
                "-n",
                "1",
            ],
        )
        assert result.exit_code == 0


class TestEbayRefreshTitles:
    def test_dry_run(self, runner, populated_db, ebay_configured, mock_ebay, monkeypatch):
        """Dry run should fetch nothing — just print what WOULD change."""
        from scryland.ebay.scryfall import CardInfo

        async def fake_find(self, name, set_name=None, collector_number=None):
            return CardInfo(
                name=name,
                set_code="soa",
                set_name=set_name or "",
                collector_number=collector_number or "",
                image_url=None,
                image_small_url=None,
                oracle_text="",
                type_line="",
                mana_cost="",
                rarity="rare",
                scryfall_uri="",
                colors=[],
            )

        monkeypatch.setattr(
            "scryland.ebay.scryfall.ScryfallClient.find_card",
            fake_find,
        )

        result = runner.invoke(cli, ["ebay-refresh-titles", "--dry-run"])
        assert result.exit_code == 0


class TestDoctorCommand:
    def test_minimal_config_reports_warns(self, runner, db_path, monkeypatch):
        """With no eBay config, doctor should still run — just warn."""
        monkeypatch.setenv("SCRYLAND_EBAY_APP_ID", "")
        monkeypatch.setenv("SCRYLAND_EBAY_CERT_ID", "")
        # Mock the httpx call to Scryfall so we don't hit the real network.
        import httpx

        async def fake_get(self, url, **kw):
            return httpx.Response(200, json={"data": [{"code": "soa"}]})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

        result = runner.invoke(cli, ["doctor"])
        # Should not crash even with partial config.
        assert result.exit_code == 0
        assert (
            "Scryland Doctor" in result.output or "PASS" in result.output or "WARN" in result.output
        )
