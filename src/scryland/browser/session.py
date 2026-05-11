"""Browser session lifecycle and login management."""

from __future__ import annotations

import logging
import random

from playwright.async_api import BrowserContext, Page, async_playwright

from scryland.browser.pages.login import LoginPage
from scryland.config import ScrylandConfig

logger = logging.getLogger("scryland")


class BrowserSession:
    """Manages Playwright browser lifecycle with persistent context.

    Uses a persistent browser context so that cookies/session data
    survive between runs. After the first manual login, subsequent
    runs may not require re-login until the session expires.
    """

    def __init__(self, config: ScrylandConfig) -> None:
        self._config = config
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        # Flipped True by Playwright's close/crash event handlers (registered
        # in start()). is_alive() reads this so we don't need an async probe
        # to detect a torn-down chromium — Page.url is a cached read that
        # never throws, and Page.is_closed() stays False until Playwright
        # observes the disconnect.
        self._dead = False

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser session not started. Call start() first.")
        return self._page

    def is_alive(self) -> bool:
        """True if the page/context are still usable. Cheap — no I/O."""
        if self._page is None or self._context is None or self._dead:
            return False
        try:
            return not self._page.is_closed()
        except Exception:
            return False

    async def start(self) -> None:
        """Launch browser with persistent context."""
        logger.info("Starting browser session...")
        # Reset in case start() is called after a previous close/crash —
        # otherwise the new session would inherit the dead flag.
        self._dead = False

        # Ensure user data directory exists
        self._config.user_data_path.mkdir(parents=True, exist_ok=True)

        # Clean up stale lock files from previous unclean shutdowns
        for lock_file in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
            lock_path = self._config.user_data_path / lock_file
            if lock_path.exists() or lock_path.is_symlink():
                lock_path.unlink(missing_ok=True)
                logger.debug("Removed stale lock file: %s", lock_file)

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._config.user_data_path),
            headless=self._config.headless,
            slow_mo=self._config.slow_mo_ms,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        # Use the first page or create one
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        self._page.set_default_timeout(self._config.browser_timeout_ms)

        if self._config.no_delays:
            # Patch page.wait_for_timeout to a no-op so existing call sites
            # across all page classes honour --no-delays without per-site
            # refactoring. Prefer `session.pause(ms)` in new code (below).
            async def _noop(*args, **kwargs):
                return None

            self._page.wait_for_timeout = _noop  # type: ignore[assignment]
            logger.info("no_delays enabled — waits and human_delay() are no-ops")

        # Flip _dead when chromium goes away. Page.url is a cached read so
        # synchronous probing can't catch a crash; these events do.
        def _mark_dead(*_args: object) -> None:
            self._dead = True

        self._context.on("close", _mark_dead)
        self._page.on("close", _mark_dead)
        self._page.on("crash", _mark_dead)

        logger.info("Browser session started")

    async def pause(self, ms: int) -> None:
        """Cosmetic pause honouring `no_delays`.

        Prefer this over page.wait_for_timeout for non-essential waits
        (letting users see state, giving Knockout time to settle). Real
        waits belong in wait_for_selector / wait_for_load_state.
        """
        if self._config.no_delays:
            return
        await self._page.wait_for_timeout(ms)

    async def dismiss_popups(self) -> None:
        """Dismiss any feedback/survey popups that TCGPlayer shows."""
        try:
            no_thanks = self._page.locator("text=No, Thanks")
            if await no_thanks.count() > 0:
                await no_thanks.click()
                logger.debug("Dismissed feedback popup")
                await self._page.wait_for_timeout(500)
        except Exception:
            logger.debug("Popup dismissal check failed", exc_info=True)

    async def human_delay(self) -> None:
        """Add a random delay to simulate human-like interaction timing."""
        if self._config.no_delays:
            return
        import asyncio  # noqa: local import to avoid circular at module level

        delay_ms = random.randint(
            self._config.min_action_delay_ms,
            self._config.max_action_delay_ms,
        )
        await asyncio.sleep(delay_ms / 1000)

    async def ensure_logged_in(self) -> None:
        """Navigate to seller portal. If not logged in, wait for manual login."""
        login_page = LoginPage(self.page, self._config)

        logger.info("Navigating to seller portal...")
        try:
            await self.page.goto(
                self._config.seller_portal_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            # Give the page a moment to settle/redirect
            await self.page.wait_for_timeout(2000)
        except Exception as exc:
            logger.debug("Navigation issue: %s — checking current page state", exc)
        final_url = self.page.url
        logger.info("Landed on: %s", final_url)

        if await login_page.is_login_required():
            logger.info("Login required — please log in via the browser window")
            await login_page.wait_for_manual_login()

            if not await login_page.verify_logged_in():
                raise RuntimeError("Login verification failed after manual login")

        await self.dismiss_popups()
        logger.info("Logged in to seller portal")

        # Now navigate to the admin page if we aren't already there
        if "/admin/" not in self.page.url:
            await self.page.goto(self._config.seller_portal_url, wait_until="networkidle")

    async def close(self) -> None:
        """Clean shutdown of browser — ensures cookies are flushed to disk."""
        if self._context:
            # Save storage state (cookies + localStorage) as backup
            try:
                import json

                state = await self._context.storage_state()
                state_path = self._config.user_data_path / "storage_state.json"
                state_path.write_text(json.dumps(state))
                state_path.chmod(0o600)
                logger.debug("Storage state saved")
            except Exception:
                pass  # Browser already closed, nothing to save

            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser session closed")
