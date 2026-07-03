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

# A cheap content fingerprint used to detect whether a Next click actually
# advanced the page. Prefers the "Viewing N-M of T" summary line the admin
# tables render (see Selectors.PAGINATION_INFO) since it's guaranteed to
# change on a real page advance. Falls back to a slice of the page body
# text — not just the first table row — because several of these tables
# render their header inside the same <tbody> as data rows (see
# get_differentials' explicit header-row skip), so "first row" risks
# fingerprinting the (unchanging) header instead of data.
_SIGNATURE_JS = """
() => {
    const viewing = document.body.innerText.match(/Viewing\\s+[\\d,]+[^\\n]*/);
    if (viewing) return viewing[0];
    return document.body.innerText.slice(0, 2000);
}
"""


class NextPageResult(StrEnum):
    ADVANCED = "advanced"  # Clicked Next and page settled cleanly
    LAST_PAGE = "last_page"  # No enabled Next control
    STALLED = "stalled"  # Clicked Next but content never changed


async def click_next_page(page: Page, timeout_ms: int = 15000) -> NextPageResult:
    """Click the paginator's Next control and wait for the new page to load.

    Waits for the page's content signature (see `_SIGNATURE_JS`) to change
    rather than for `networkidle` — an already-idle page resolves
    `wait_for_load_state("networkidle")` immediately, before the pagination
    XHR even fires, which let a queued double-advance silently skip a page.

    Returns:
        ADVANCED  — content signature changed, safe to keep scraping.
        LAST_PAGE — no enabled Next control found (we're done).
        STALLED   — Next was clicked but content never changed. Caller
                    must NOT treat partial results as complete.
    """
    before = await retry_on_flaky(
        lambda: page.evaluate(_SIGNATURE_JS),
        page=page,
        label="click_next_page signature",
    )

    outcome = await retry_on_flaky(
        lambda: page.evaluate(_NEXT_PAGE_JS),
        page=page,
        label="click_next_page",
    )
    if outcome != "clicked":
        return NextPageResult.LAST_PAGE

    try:
        await page.wait_for_function(
            f"(before) => ({_SIGNATURE_JS})() !== before && ({_SIGNATURE_JS})() !== ''",
            before,
            timeout=timeout_ms,
        )
    except Exception:
        logger.warning("Pagination Next click did not change page content within %dms", timeout_ms)
        return NextPageResult.STALLED

    return NextPageResult.ADVANCED
