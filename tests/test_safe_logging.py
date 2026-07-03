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
        """Non-sensitive args still format correctly — the filter now
        pre-formats the message (clearing record.args) rather than leaving
        the raw args tuple for a later % substitution."""
        f = SensitiveDataFilter()
        record = self._make_record("count=%d", (42,))
        f.filter(record)
        assert record.getMessage() == "count=42"

    def test_always_returns_true(self):
        f = SensitiveDataFilter()
        record = self._make_record("test")
        assert f.filter(record) is True

    def test_percent_style_arg_is_redacted_without_formatting_error(self):
        """Redacting the raw '%s'-template can match-and-consume the
        placeholder itself (e.g. "token=%s" matches the sensitive-key
        pattern whole), corrupting the template so %-formatting later
        raises. Must redact the fully formatted message instead."""
        f = SensitiveDataFilter()
        record = self._make_record("token=%s", ("SECRET",))
        f.filter(record)
        formatted = record.getMessage()  # must not raise
        assert "SECRET" not in formatted
        assert "[REDACTED]" in formatted

    def test_mismatched_placeholders_do_not_raise_out_of_filter(self):
        """Filter.filter() runs OUTSIDE logging's emit() try/except safety
        net. A malformed log call (wrong %s/arg count) must not raise out
        of filter() into the caller — the record must be left untouched so
        logging's own emit-time error handling ("--- Logging error ---")
        applies, same degradation as stock logging."""
        f = SensitiveDataFilter()
        record = self._make_record("a=%s b=%s", ("only-one",))
        assert f.filter(record) is True  # must not raise
        # Record untouched: template + args intact for emit-time handling.
        assert record.msg == "a=%s b=%s"
        assert record.args == ("only-one",)

    def test_mismatched_placeholders_handled_like_stock_logging(self, capsys):
        """End-to-end: a malformed log call through a real logger+handler
        with the filter attached must not raise, and must be routed to
        logging's handleError path (stderr), not crash the caller."""
        logger = logging.getLogger("scryland.test_safe_logging_malformed")
        logger.handlers.clear()
        logger.propagate = False
        handler = logging.StreamHandler()
        handler.addFilter(SensitiveDataFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.info("a=%s b=%s", "only-one")  # must not raise
        finally:
            logger.handlers.clear()
        err = capsys.readouterr().err
        assert "Logging error" in err
