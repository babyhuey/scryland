"""Retry helpers for transient Playwright errors.

TCGPlayer's admin pages re-render aggressively (Knockout.js bindings, ajax
table refreshes, save-redirects). Playwright often raises:

- "Execution context was destroyed, most likely because of a navigation"
- "Unable to adopt element handle from a different document"
- "Cannot find context with specified id"
- "net::ERR_ABORTED" on navigation races

These are all "the page moved out from under you, try again" failures.
`retry_on_flaky` wraps a coroutine call so transient faults get retried
after a brief settle, while real errors still propagate.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from playwright.async_api import Page

logger = logging.getLogger("scryland")

_FLAKY_MARKERS = (
    "Execution context was destroyed",
    "Unable to adopt element handle from a different document",
    "Cannot find context with specified id",
    "ERR_ABORTED",
    # Pricing/inventory tables occasionally render post-load; treat a
    # "not rendered" right after a navigation as flaky so the reload
    # path can try again. "Not rendered" is specifically the timing
    # case; "condition not found" (the row is genuinely absent) is a
    # data mismatch that shouldn't trigger retry.
    "Pricing table not rendered",
)

# Deliberately excluded from _FLAKY_MARKERS — a closed page/context/browser
# is a real error, not a transient one, so it's kept separate rather than
# relying on positional slicing (`_FLAKY_MARKERS[:-1]`) to exclude it, which
# broke silently whenever a marker was appended above it.
_CLOSED_MARKER = "Target page, context or browser has been closed"

# Errors where the page is probably hung / stuck loading, and a hard
# reload is more likely to help than another attempt on the same nav.
_HANG_MARKERS = (
    "Timeout",
    "exceeded",
    "net::ERR_TIMED_OUT",
    "net::ERR_CONNECTION_TIMED_OUT",
)


def _is_flaky(exc: BaseException) -> bool:
    msg = str(exc)
    # The "Target page … has been closed" text is a hard failure; strip it out.
    if _CLOSED_MARKER in msg:
        return False
    if any(marker in msg for marker in _FLAKY_MARKERS):
        return True
    return _is_hang(exc)


def _is_hang(exc: BaseException) -> bool:
    msg = str(exc)
    return any(marker in msg for marker in _HANG_MARKERS)


T = TypeVar("T")


async def retry_on_flaky(
    fn: Callable[[], Awaitable[T]],
    *,
    page: Page,
    attempts: int = 3,
    settle_timeout_ms: int = 8000,
    label: str = "operation",
) -> T:
    """Call `fn()` up to `attempts` times, retrying on transient page errors.

    Between attempts we wait for DOMContentLoaded + networkidle so the page
    has time to recover. When the failure looks like a hung page
    (timeout/ERR_TIMED_OUT), we hard-reload the frame before retrying.
    Non-flaky exceptions propagate immediately.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            if not _is_flaky(exc):
                raise
            last_exc = exc
            if attempt == attempts:
                break
            hung = _is_hang(exc)
            logger.debug(
                "%s failed on attempt %d/%d (%s%s) — settling and retrying",
                label,
                attempt,
                attempts,
                type(exc).__name__,
                " [hang]" if hung else "",
            )
            # Hung page: force a reload. Ordinary flaky: just wait for load.
            if hung:
                try:
                    logger.warning(
                        "%s appears hung — forcing page reload before retry",
                        label,
                    )
                    await page.reload(wait_until="domcontentloaded", timeout=settle_timeout_ms * 2)
                except Exception:
                    # Reload itself failed — last-resort: navigate to
                    # about:blank, then let the retried fn() drive the
                    # navigation fresh. Log the blank-nav failure too so
                    # we can tell if both recovery paths are broken.
                    try:
                        await page.goto("about:blank", timeout=5000)
                    except Exception:
                        logger.debug(
                            "page.reload and about:blank both failed — retrying on existing page",
                            exc_info=True,
                        )
            else:
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=settle_timeout_ms,
                    )
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=settle_timeout_ms,
                    )
                except Exception:
                    pass
    assert last_exc is not None
    logger.warning("%s failed after %d attempts: %s", label, attempts, last_exc)
    raise last_exc
