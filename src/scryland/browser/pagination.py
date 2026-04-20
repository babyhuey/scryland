"""Shared pagination helper for TCGPlayer admin tables.

The admin UI uses the same Next-link pattern across inventory, orders, and
reports. This module centralises the "click Next until disabled" logic and
— importantly — signals when the advance was *incomplete* (timeout, stale
page) so callers can refuse to treat partial results as authoritative.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from playwright.async_api import Page

from scryland.browser.flaky import retry_on_flaky

logger = logging.getLogger("scryland")


_NEXT_PAGE_JS = """
() => {
    const candidates = document.querySelectorAll('a, input, button');
    for (const el of candidates) {
        const t = (el.textContent || el.value || '').trim();
        if (t !== 'Next' && t !== 'Next >' && t !== '>') continue;
        if (el.disabled) return 'disabled';
        const cls = (el.className || '') + '';
        if (cls.includes('disabled')) return 'disabled';
        el.click();
        return 'clicked';
    }
    return 'no-next';
}
"""


class NextPageResult(StrEnum):
    ADVANCED = "advanced"  # Clicked Next and page settled cleanly
    LAST_PAGE = "last_page"  # No enabled Next control
    STALLED = "stalled"  # Clicked Next but load state never settled


async def click_next_page(page: Page, timeout_ms: int = 15000) -> NextPageResult:
    """Click the paginator's Next control and wait for the new page to load.

    Returns:
        ADVANCED  — new page rendered, safe to keep scraping.
        LAST_PAGE — no enabled Next control found (we're done).
        STALLED   — Next was clicked but load state did not settle. Caller
                    must NOT treat partial results as complete.
    """
    outcome = await retry_on_flaky(
        lambda: page.evaluate(_NEXT_PAGE_JS),
        page=page,
        label="click_next_page",
    )
    if outcome != "clicked":
        return NextPageResult.LAST_PAGE

    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        logger.warning("Pagination Next click did not settle within %dms", timeout_ms)
        return NextPageResult.STALLED

    return NextPageResult.ADVANCED
