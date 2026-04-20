"""Tests for inventory page scraping."""

from decimal import Decimal

from scryland.browser.pages.inventory import _norm_name_for_match, _parse_price


class TestNormNameForMatch:
    def test_collapses_dfc_to_front_face(self):
        assert _norm_name_for_match("Grave Researcher // Reanimate") == "grave researcher"
        assert _norm_name_for_match("Grave Researcher") == "grave researcher"

    def test_strips_parenthetical(self):
        assert _norm_name_for_match("Hop to It (Borderless)") == "hop to it"

    def test_case_and_punctuation(self):
        assert _norm_name_for_match("Forum's Favor!") == "forum s favor"


class TestParsePrice:
    def test_parses_dollar_amount(self):
        assert _parse_price("$1.50") == Decimal("1.50")

    def test_parses_without_dollar_sign(self):
        assert _parse_price("1.50") == Decimal("1.50")

    def test_parses_with_comma(self):
        assert _parse_price("$1,234.56") == Decimal("1234.56")

    def test_returns_none_for_empty(self):
        assert _parse_price("") is None
        assert _parse_price(None) is None

    def test_returns_none_for_dash(self):
        assert _parse_price("-") is None

    def test_returns_none_for_na(self):
        assert _parse_price("N/A") is None
        assert _parse_price("n/a") is None

    def test_returns_none_for_garbage(self):
        assert _parse_price("abc") is None

    def test_handles_whitespace(self):
        assert _parse_price("  $1.50  ") == Decimal("1.50")

    def test_parses_zero(self):
        assert _parse_price("$0.00") == Decimal("0.00")

    def test_parses_large_number(self):
        assert _parse_price("$50,000.00") == Decimal("50000.00")

    def test_parses_price_with_shipping(self):
        assert _parse_price("$20.34\n+ Shipping: $0.99") == Decimal("20.34")

    def test_parses_price_with_shipping_inline(self):
        assert _parse_price("$16.68 + Shipping: $5.99") == Decimal("16.68")
