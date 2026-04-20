"""Tests for safety guardrails."""

from decimal import Decimal

import pytest

from scryland.config import ScrylandConfig
from scryland.models import Listing, PriceUpdate, UpdateStatus
from scryland.pricing.guardrails import PriceGuardrails


@pytest.fixture
def guardrails(config):
    return PriceGuardrails(config)


def _make_update(
    old: str = "10.00",
    new: str = "9.00",
    pct: float = -10.0,
    name: str = "Test Card",
) -> PriceUpdate:
    return PriceUpdate(
        listing=Listing(product_name=name, current_price=Decimal(old)),
        new_price=Decimal(new),
        old_price=Decimal(old),
        change_pct=pct,
    )


class TestCheck:
    def test_small_change_no_confirmation(self, guardrails):
        update = _make_update(old="10.00", new="9.50", pct=-5.0)
        result = guardrails.check(update)
        assert result.status == UpdateStatus.PENDING
        assert result.requires_confirmation is False

    def test_large_decrease_requires_confirmation(self, guardrails):
        update = _make_update(old="10.00", new="8.00", pct=-20.0)
        result = guardrails.check(update)
        assert result.status == UpdateStatus.PENDING
        assert result.requires_confirmation is True

    def test_large_increase_requires_confirmation(self, guardrails):
        update = _make_update(old="10.00", new="12.00", pct=20.0)
        result = guardrails.check(update)
        assert result.requires_confirmation is True

    def test_exact_threshold_no_confirmation(self, guardrails):
        update = _make_update(old="10.00", new="9.00", pct=-10.0)
        result = guardrails.check(update)
        assert result.requires_confirmation is False

    def test_just_over_threshold_requires_confirmation(self, guardrails):
        update = _make_update(old="10.00", new="8.90", pct=-11.0)
        result = guardrails.check(update)
        assert result.requires_confirmation is True

    def test_rejects_zero_price(self, guardrails):
        update = _make_update(old="10.00", new="0.00", pct=-100.0)
        result = guardrails.check(update)
        assert result.status == UpdateStatus.REJECTED

    def test_rejects_negative_price(self, guardrails):
        update = _make_update(old="10.00", new="-1.00", pct=-110.0)
        result = guardrails.check(update)
        assert result.status == UpdateStatus.REJECTED

    def test_rejects_below_floor(self, guardrails):
        update = _make_update(old="0.50", new="0.10", pct=-80.0)
        result = guardrails.check(update)
        assert result.status == UpdateStatus.REJECTED

    def test_allows_at_floor(self, guardrails):
        update = _make_update(old="0.50", new="0.25", pct=-50.0)
        result = guardrails.check(update)
        assert result.status == UpdateStatus.PENDING  # Not rejected
        assert result.requires_confirmation is True  # But flagged (50% change)

    def test_custom_threshold(self):
        config = ScrylandConfig(max_price_change_pct=5.0)
        guardrails = PriceGuardrails(config)
        update = _make_update(old="10.00", new="9.20", pct=-8.0)
        result = guardrails.check(update)
        assert result.requires_confirmation is True  # 8% > 5% threshold


class TestValidateBatch:
    def test_batch_checks_all_updates(self, guardrails):
        updates = [
            _make_update(old="10.00", new="9.50", pct=-5.0),
            _make_update(old="10.00", new="8.00", pct=-20.0),
            _make_update(old="0.50", new="0.10", pct=-80.0),
        ]
        results = guardrails.validate_batch(updates)
        assert len(results) == 3
        # First: small change, no confirmation
        assert results[0].requires_confirmation is False
        assert results[0].status == UpdateStatus.PENDING
        # Second: large change, needs confirmation
        assert results[1].requires_confirmation is True
        # Third: below floor, rejected
        assert results[2].status == UpdateStatus.REJECTED

    def test_empty_batch(self, guardrails):
        results = guardrails.validate_batch([])
        assert results == []


class TestPromptConfirmation:
    def test_returns_bool(self, guardrails, monkeypatch):
        update = _make_update(old="10.00", new="8.00", pct=-20.0)
        # Mock the Confirm.ask to return True
        monkeypatch.setattr("scryland.pricing.guardrails.Confirm.ask", lambda *a, **kw: True)
        assert guardrails.prompt_confirmation(update) is True

    def test_returns_false_on_deny(self, guardrails, monkeypatch):
        update = _make_update(old="10.00", new="8.00", pct=-20.0)
        monkeypatch.setattr("scryland.pricing.guardrails.Confirm.ask", lambda *a, **kw: False)
        assert guardrails.prompt_confirmation(update) is False
