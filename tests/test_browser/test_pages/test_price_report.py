"""Tests for the Price Differential Report page's pure dedupe logic."""

from __future__ import annotations

from scryland.browser.pages.price_report import _dedupe_new_rows


def _row(product_name: str, set_name: str, condition: str = "Near Mint") -> dict:
    return {"product_name": product_name, "set_name": set_name, "condition": condition}


class TestDedupeNewRows:
    def test_keeps_second_printing_of_same_card_and_condition(self):
        # Regression test: the old (product_name, condition) key dropped a
        # second printing of the same card/condition from a different set.
        rows = [
            _row("Reprieve", "Ravnica Allegiance"),
            _row("Reprieve", "Secret Lair Drop Series"),
        ]
        seen: set[tuple[str, str, str]] = set()
        new_rows = _dedupe_new_rows(rows, seen)
        assert new_rows == rows
        assert len(seen) == 2

    def test_drops_true_duplicate_within_same_set(self):
        rows = [_row("Reprieve", "Ravnica Allegiance"), _row("Reprieve", "Ravnica Allegiance")]
        seen: set[tuple[str, str, str]] = set()
        new_rows = _dedupe_new_rows(rows, seen)
        assert len(new_rows) == 1

    def test_drops_duplicate_seen_on_a_prior_page(self):
        seen = {("Reprieve", "Ravnica Allegiance", "Near Mint")}
        rows = [_row("Reprieve", "Ravnica Allegiance"), _row("Island", "Core Set 2021")]
        new_rows = _dedupe_new_rows(rows, seen)
        assert new_rows == [_row("Island", "Core Set 2021")]

    def test_different_condition_same_card_and_set_not_deduped(self):
        rows = [
            _row("Reprieve", "Ravnica Allegiance", "Near Mint"),
            _row("Reprieve", "Ravnica Allegiance", "Lightly Played"),
        ]
        seen: set[tuple[str, str, str]] = set()
        new_rows = _dedupe_new_rows(rows, seen)
        assert len(new_rows) == 2
