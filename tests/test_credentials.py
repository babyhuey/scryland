"""Tests for encrypted credential storage."""

import pytest

from scryland.credentials import (
    clear_credentials,
    credentials_exist,
    load_credentials,
    save_credentials,
)


@pytest.fixture(autouse=True)
def clean_cred_files(tmp_path, monkeypatch):
    """Run all credential tests in a temp directory."""
    monkeypatch.chdir(tmp_path)
    yield
    # Cleanup
    for f in [".scryland_credentials", ".scryland_salt"]:
        p = tmp_path / f
        if p.exists():
            p.unlink()


class TestSaveAndLoad:
    def test_round_trip(self):
        save_credentials("user@test.com", "s3cret!", "mypass")
        result = load_credentials("mypass")
        assert result == ("user@test.com", "s3cret!")

    def test_wrong_passphrase(self):
        save_credentials("user@test.com", "s3cret!", "mypass")
        result = load_credentials("wrongpass")
        assert result is None

    def test_file_permissions(self, tmp_path):
        save_credentials("user@test.com", "s3cret!", "mypass")
        cred_stat = (tmp_path / ".scryland_credentials").stat()
        salt_stat = (tmp_path / ".scryland_salt").stat()
        assert oct(cred_stat.st_mode)[-3:] == "600"
        assert oct(salt_stat.st_mode)[-3:] == "600"

    def test_overwrite(self):
        save_credentials("old@test.com", "old", "pass1")
        save_credentials("new@test.com", "new", "pass2")
        assert load_credentials("pass1") is None
        assert load_credentials("pass2") == ("new@test.com", "new")


class TestCredentialsExist:
    def test_no_files(self):
        assert credentials_exist() is False

    def test_with_files(self):
        save_credentials("u", "p", "k")
        assert credentials_exist() is True


class TestClearCredentials:
    def test_clear(self):
        save_credentials("u", "p", "k")
        assert credentials_exist() is True
        clear_credentials()
        assert credentials_exist() is False

    def test_clear_when_none(self):
        clear_credentials()  # Should not raise
