"""Tests for CLI interface."""

import csv

from click.testing import CliRunner

from scryland.cli import cli


class TestCLI:
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Scryland" in result.output

    def test_optimize_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["optimize", "--help"])
        assert result.exit_code == 0
        assert "optimization" in result.output.lower()

    def test_explore_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["explore", "--help"])
        assert result.exit_code == 0
        assert "exploration" in result.output.lower() or "explore" in result.output.lower()

    def test_login_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["login", "--help"])
        assert result.exit_code == 0

    def test_dry_run_flag_on_optimize(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["optimize", "--help"])
        assert result.exit_code == 0
        assert "--dry-run" in result.output

    def test_add_inventory_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["add-inventory", "--help"])
        assert result.exit_code == 0
        assert "--skip-lands" in result.output
        assert "--min-price" in result.output
        assert "--csv-min-price" in result.output
        assert "--no-save" in result.output
        assert "--limit" in result.output

    def test_csv_optimize_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["csv-optimize", "--help"])
        assert result.exit_code == 0
        assert "--dry-run" in result.output

    def test_credentials_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["credentials", "--help"])
        assert result.exit_code == 0
        assert "set" in result.output
        assert "clear" in result.output
        assert "status" in result.output

    def test_credentials_status_no_creds(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["credentials", "status"])
        assert result.exit_code == 0
        assert "No stored credentials" in result.output

    def test_log_level_option(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--log-level", "DEBUG", "--help"])
        assert result.exit_code == 0


class TestAddInventoryDryRun:
    def _make_csv(self, tmp_path, cards):
        csv_path = tmp_path / "test.csv"
        fieldnames = [
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
            "Price (USD)",
            "Price (EUR)",
            "Price (USD Foil)",
            "Price (EUR Foil)",
            "Price (USD Etched)",
            "Price (EUR Etched)",
            "Scryfall ID",
            "Container Type",
            "Container Name",
        ]
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for card in cards:
                row = dict.fromkeys(fieldnames, "")
                row.update(card)
                writer.writerow(row)
        return csv_path

    def test_dry_run_shows_table(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [
                {
                    "Card Name": "Lightning Bolt",
                    "Set Name": "Core 2021",
                    "Language": "en",
                    "Quantity": "1",
                    "Condition": "NM",
                    "Finish": "nonfoil",
                    "Price (USD)": "1.50",
                },
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["add-inventory", str(csv_path), "--dry-run"])
        assert result.exit_code == 0
        assert "Lightning Bolt" in result.output
        assert "1 cards to add" in result.output

    def test_skip_lands(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [
                {
                    "Card Name": "Mountain",
                    "Set Name": "M21",
                    "Language": "en",
                    "Quantity": "1",
                    "Condition": "NM",
                    "Finish": "nonfoil",
                    "Price (USD)": "0.10",
                },
                {
                    "Card Name": "Lightning Bolt",
                    "Set Name": "M21",
                    "Language": "en",
                    "Quantity": "1",
                    "Condition": "NM",
                    "Finish": "nonfoil",
                    "Price (USD)": "1.50",
                },
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["add-inventory", str(csv_path), "--skip-lands", "--dry-run"])
        assert result.exit_code == 0
        assert "Skipped 1 basic land" in result.output
        assert "Lightning Bolt" in result.output
        assert "Mountain" not in result.output.split("Skipped")[0]  # Not in the table

    def test_skip_zero_price(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [
                {
                    "Card Name": "Token",
                    "Set Name": "M21",
                    "Language": "en",
                    "Quantity": "1",
                    "Condition": "NM",
                    "Finish": "nonfoil",
                    "Price (USD)": "0",
                },
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["add-inventory", str(csv_path), "--dry-run"])
        assert result.exit_code == 0
        assert "Skipped 1 cards with $0.00" in result.output

    def test_csv_min_price_filter(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [
                {
                    "Card Name": "Cheap Card",
                    "Set Name": "M21",
                    "Language": "en",
                    "Quantity": "1",
                    "Condition": "NM",
                    "Finish": "nonfoil",
                    "Price (USD)": "0.05",
                },
                {
                    "Card Name": "Good Card",
                    "Set Name": "M21",
                    "Language": "en",
                    "Quantity": "1",
                    "Condition": "NM",
                    "Finish": "nonfoil",
                    "Price (USD)": "5.00",
                },
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["add-inventory", str(csv_path), "--min-price", "1.0", "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Good Card" in result.output

    def test_csv_min_price_disabled(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [
                {
                    "Card Name": "Cheap Card",
                    "Set Name": "M21",
                    "Language": "en",
                    "Quantity": "1",
                    "Condition": "NM",
                    "Finish": "nonfoil",
                    "Price (USD)": "0.05",
                },
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["add-inventory", str(csv_path), "--csv-min-price", "0", "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Cheap Card" in result.output

    def test_skips_non_english(self, tmp_path):
        csv_path = self._make_csv(
            tmp_path,
            [
                {
                    "Card Name": "Rayo",
                    "Set Name": "M21",
                    "Language": "es",
                    "Quantity": "1",
                    "Condition": "NM",
                    "Finish": "nonfoil",
                    "Price (USD)": "1.00",
                },
            ],
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["add-inventory", str(csv_path), "--dry-run"])
        assert result.exit_code == 0
        assert "No cards found" in result.output
