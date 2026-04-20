"""Tests for configuration."""

from scryland.config import ScrylandConfig


def test_default_config():
    config = ScrylandConfig()
    assert config.max_price_change_pct == 10.0
    assert config.min_price_floor == 0.25
    assert config.dry_run is False
    assert config.headless is False
    assert config.slow_mo_ms == 100


def test_config_overrides():
    config = ScrylandConfig(max_price_change_pct=5.0, dry_run=True, headless=True)
    assert config.max_price_change_pct == 5.0
    assert config.dry_run is True
    assert config.headless is True


def test_user_data_path():
    config = ScrylandConfig(user_data_dir="/tmp/test_session")
    assert str(config.user_data_path) == "/tmp/test_session"
