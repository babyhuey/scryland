"""Tests for retry_on_flaky + _is_flaky / _is_hang predicates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scryland.browser.flaky import _is_flaky, _is_hang, retry_on_flaky


class TestIsFlaky:
    def test_context_destroyed_is_flaky(self):
        assert _is_flaky(Exception("Execution context was destroyed, most likely..."))

    def test_stale_element_is_flaky(self):
        assert _is_flaky(Exception("Unable to adopt element handle from a different document"))

    def test_err_aborted_is_flaky(self):
        assert _is_flaky(Exception("Page.goto: net::ERR_ABORTED"))

    def test_page_closed_is_not_flaky(self):
        assert not _is_flaky(Exception("Target page, context or browser has been closed"))

    def test_timeout_is_flaky_via_hang(self):
        assert _is_flaky(Exception("Timeout 30000ms exceeded"))

    def test_random_error_not_flaky(self):
        assert not _is_flaky(Exception("ValueError: something else"))


class TestIsHang:
    def test_timeout_exceeded(self):
        assert _is_hang(Exception("Timeout 30000ms exceeded"))

    def test_timed_out(self):
        assert _is_hang(Exception("net::ERR_TIMED_OUT"))

    def test_connection_timeout(self):
        assert _is_hang(Exception("net::ERR_CONNECTION_TIMED_OUT"))

    def test_not_a_hang(self):
        assert not _is_hang(Exception("Execution context was destroyed"))


class TestRetryOnFlaky:
    async def test_success_first_try(self):
        page = MagicMock()
        fn = AsyncMock(return_value="ok")
        result = await retry_on_flaky(fn, page=page, label="test")
        assert result == "ok"
        fn.assert_awaited_once()

    async def test_non_flaky_propagates_immediately(self):
        page = MagicMock()
        fn = AsyncMock(side_effect=ValueError("hard error"))
        with pytest.raises(ValueError):
            await retry_on_flaky(fn, page=page, label="test")
        fn.assert_awaited_once()

    async def test_retries_on_flaky_and_succeeds(self):
        page = MagicMock()
        page.wait_for_load_state = AsyncMock()
        fn = AsyncMock(
            side_effect=[
                Exception("Execution context was destroyed"),
                "success",
            ]
        )
        result = await retry_on_flaky(fn, page=page, attempts=3, label="test")
        assert result == "success"
        assert fn.await_count == 2
        # After failure, should wait for load state.
        assert page.wait_for_load_state.await_count >= 1

    async def test_hang_triggers_reload(self):
        page = MagicMock()
        page.reload = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.goto = AsyncMock()
        fn = AsyncMock(
            side_effect=[
                Exception("Timeout 30000ms exceeded"),
                "success",
            ]
        )
        result = await retry_on_flaky(fn, page=page, attempts=3, label="test")
        assert result == "success"
        page.reload.assert_awaited()  # hard reload after hang

    async def test_all_attempts_fail_raises(self):
        page = MagicMock()
        page.wait_for_load_state = AsyncMock()
        fn = AsyncMock(side_effect=Exception("Execution context was destroyed"))
        with pytest.raises(Exception, match="context was destroyed"):
            await retry_on_flaky(fn, page=page, attempts=2, label="test")
        assert fn.await_count == 2

    async def test_reload_failure_falls_back_to_blank(self):
        page = MagicMock()
        page.reload = AsyncMock(side_effect=Exception("reload failed"))
        page.goto = AsyncMock()
        fn = AsyncMock(
            side_effect=[
                Exception("Timeout exceeded"),
                "success",
            ]
        )
        result = await retry_on_flaky(fn, page=page, attempts=3, label="test")
        assert result == "success"
        # Fallback to about:blank when reload fails.
        page.goto.assert_awaited()
