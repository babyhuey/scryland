"""Tests for the pure matching/verification logic in the add-inventory flow."""

from __future__ import annotations

from scryland.browser.pages.add_inventory import _is_plausible_price, _resolve_best_match


class TestResolveBestMatch:
    def test_lone_partial_match_is_not_promoted(self):
        # Regression test: a single partial-name match (score 1) used to be
        # promoted to score 3 ("safe to auto-add") just for being the only
        # candidate on the page. It must stay at its real tier so the
        # caller's non-safe (skip/manual) path applies.
        matches = [{"name": "Some Other Card", "score": 1}]
        best, score = _resolve_best_match(matches)
        assert score == 1
        assert best["name"] == "Some Other Card"

    def test_lone_exact_match_stays_at_score_2(self):
        matches = [{"name": "Lightning Bolt", "score": 2}]
        best, score = _resolve_best_match(matches)
        assert score == 2

    def test_lone_collector_number_match_stays_at_score_3(self):
        matches = [{"name": "Lightning Bolt", "score": 3}]
        _best, score = _resolve_best_match(matches)
        assert score == 3

    def test_multiple_matches_picks_highest_score(self):
        matches = [
            {"name": "Lightning Bolt (Borderless)", "score": 1},
            {"name": "Lightning Bolt", "score": 3},
            {"name": "Lightning Bolt (Showcase)", "score": 2},
        ]
        best, score = _resolve_best_match(matches)
        assert score == 3
        assert best["name"] == "Lightning Bolt"

    def test_tie_keeps_first_occurrence(self):
        matches = [
            {"name": "First", "score": 2},
            {"name": "Second", "score": 2},
        ]
        best, score = _resolve_best_match(matches)
        assert score == 2
        assert best["name"] == "First"


class TestIsPlausiblePrice:
    def test_valid_price(self):
        assert _is_plausible_price("1.50") is True

    def test_valid_price_with_dollar_sign(self):
        assert _is_plausible_price("$1.50") is True

    def test_valid_price_with_commas(self):
        assert _is_plausible_price("$1,234.56") is True

    def test_empty_string_not_plausible(self):
        assert _is_plausible_price("") is False

    def test_none_not_plausible(self):
        assert _is_plausible_price(None) is False

    def test_zero_not_plausible(self):
        # A Match click that "succeeds" but leaves the price at 0 is not a
        # verified success — the value must be a real positive price.
        assert _is_plausible_price("0") is False
        assert _is_plausible_price("0.00") is False

    def test_negative_not_plausible(self):
        assert _is_plausible_price("-1.50") is False

    def test_garbage_not_plausible(self):
        assert _is_plausible_price("Match") is False
