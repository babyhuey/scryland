"""Tests for sensitive data redaction in logging."""

import logging

from scryland.safe_logging import SensitiveDataFilter


class TestSensitiveDataFilter:
    def _make_record(self, msg, args=None):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=args,
            exc_info=None,
        )
        return record

    def test_redacts_token_in_message(self):
        f = SensitiveDataFilter()
        record = self._make_record("token=abc123secret")
        f.filter(record)
        assert "abc123secret" not in record.msg
        assert "[REDACTED]" in record.msg

    def test_redacts_bearer_token(self):
        f = SensitiveDataFilter()
        record = self._make_record("Authorization: Bearer eyJhbGciOi...")
        f.filter(record)
        assert "eyJhbGciOi" not in record.msg

    def test_redacts_cookie_value(self):
        f = SensitiveDataFilter()
        record = self._make_record("cookie=session_abc123xyz")
        f.filter(record)
        assert "session_abc123xyz" not in record.msg

    def test_leaves_normal_message(self):
        f = SensitiveDataFilter()
        record = self._make_record("Processing product: Lightning Bolt")
        f.filter(record)
        assert record.msg == "Processing product: Lightning Bolt"

    def test_handles_tuple_args(self):
        f = SensitiveDataFilter()
        record = self._make_record("key=%s", ("token=secret123",))
        f.filter(record)
        assert "secret123" not in str(record.args)

    def test_handles_dict_args(self):
        f = SensitiveDataFilter()
        # Python logging passes dict args inside a tuple with one element
        record = self._make_record("%(key)s", ({"key": "password=hunter2"},))
        # Manually set args to a dict as logging does internally
        record.args = {"key": "password=hunter2"}
        f.filter(record)
        assert "hunter2" not in str(record.args)

    def test_handles_none_args(self):
        f = SensitiveDataFilter()
        record = self._make_record("no args")
        record.args = None
        f.filter(record)
        assert record.msg == "no args"

    def test_non_string_args_untouched(self):
        f = SensitiveDataFilter()
        record = self._make_record("count=%d", (42,))
        f.filter(record)
        assert record.args == (42,)

    def test_always_returns_true(self):
        f = SensitiveDataFilter()
        record = self._make_record("test")
        assert f.filter(record) is True
