"""Login page detection and manual login flow."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.async_api import Page

from scryland.config import ScrylandConfig

logger = logging.getLogger("scryland")


def _is_admin_page(url: str) -> bool:
    """Check if a URL is on the TCGPlayer admin/seller portal (ignoring query params)."""
    parsed = urlparse(url.lower())
    path = parsed.path
    host = parsed.hostname or ""

    if "store.tcgplayer.com" in host and path.startswith("/admin"):
        return True
    if "sellerportal.tcgplayer.com" in host:
        return True
    return False


def _is_login_page(url: str) -> bool:
    """Check if a URL is a login/auth page (checking path only, not query params)."""
    parsed = urlparse(url.lower())
    path = parsed.path

    login_path_indicators = [
        "/oauth",
        "/account/login",
        "/signin",
        "/auth",
    ]
    for indicator in login_path_indicators:
        if indicator in path:
            return True

    host = parsed.hostname or ""
    if "accounts.tcgplayer.com" in host:
        return True

    return False


class LoginPage:
    """Handles login detection and manual login waiting."""

    def __init__(self, page: Page, config: ScrylandConfig) -> None:
        self._page = page
        self._config = config

    async def is_login_required(self) -> bool:
        """Check if the current page is a login/auth page.

        Checks the URL path (not query params) to avoid false positives
        from returnUrl parameters containing /admin paths.
        """
        current_url = self._page.url
        logger.debug("Checking login status, current URL: %s", current_url)

        if _is_admin_page(current_url):
            logger.debug("On admin page — login not required")
            return False

        if _is_login_page(current_url):
            logger.debug("On login page — login required")
            return True

        logger.debug("Unknown page — assuming login required")
        return True

    async def wait_for_manual_login(self, timeout_ms: int = 300_000) -> None:
        """Wait for user to complete manual login.

        Prints instructions and waits for the URL to change to the
        seller portal, indicating successful login.

        Args:
            timeout_ms: Maximum time to wait for login (default 5 minutes).
        """
        from rich.console import Console

        console = Console()
        console.print()
        console.print("[bold yellow]Manual login required![/bold yellow]")
        console.print("Please log in to your TCGPlayer account in the browser window.")
        console.print(f"Waiting up to {timeout_ms // 60_000} minutes...")
        console.print()

        # Try to check "Remember Me" on the login page automatically
        try:
            remember_me = self._page.locator("text=Remember me")
            if await remember_me.count() > 0:
                await remember_me.click()
                logger.debug("Checked 'Remember Me' on login page")
        except Exception:
            logger.debug("Could not find/check 'Remember Me'", exc_info=True)

        # Try auto-login with stored credentials
        if await self._try_auto_login():
            return

        import asyncio

        # Poll the URL periodically — more reliable than wait_for_url
        # since the login flow may redirect through multiple pages
        poll_interval_ms = 2000
        elapsed = 0
        while elapsed < timeout_ms:
            current = self._page.url
            if _is_admin_page(current):
                logger.info("Login detected — on admin page: %s", current)
                return
            await asyncio.sleep(poll_interval_ms / 1000)
            elapsed += poll_interval_ms

        raise RuntimeError("Login timed out. Please try again with `scryland login`.")

    async def _try_auto_login(self) -> bool:
        """Attempt auto-login using stored encrypted credentials.

        The passphrase can be provided via:
        1. SCRYLAND_PASSPHRASE environment variable (set once per shell session)
        2. Interactive prompt

        Returns True if auto-login succeeded and we're now on the admin page.
        """
        import os

        from pathlib import Path

        from scryland.credentials import credentials_exist, load_credentials

        base_dir = Path(self._config.db_path).parent
        if not credentials_exist(base_dir=base_dir):
            return False

        from rich.console import Console

        console = Console()
        console.print("[cyan]Stored credentials found. Attempting auto-login...[/cyan]")

        # Try environment variable first (non-interactive)
        passphrase = os.environ.get("SCRYLAND_PASSPHRASE")
        if not passphrase:
            try:
                from rich.prompt import Prompt

                passphrase = Prompt.ask("Enter passphrase to decrypt credentials", password=True)
            except (EOFError, OSError):
                console.print(
                    "[red]Cannot read passphrase (non-interactive). "
                    "Set SCRYLAND_PASSPHRASE env var.[/red]"
                )
                return False

        creds = load_credentials(passphrase, base_dir=base_dir)
        if not creds:
            console.print("[red]Could not decrypt credentials. Falling back to manual login.[/red]")
            return False

        username, password = creds
        logger.info("Auto-filling login form...")

        try:
            import random

            # Find and fill the email/username field
            email_input = self._page.locator(
                "input[type='email'], input[name='Email'], input[id='Email']"
            ).first
            if await email_input.count() > 0:
                await email_input.fill(username)
            else:
                email_input = self._page.locator("input[type='text']").first
                await email_input.fill(username)

            # Human-like pause between fields
            await self._page.wait_for_timeout(random.randint(500, 1500))

            # Find and fill the password field
            pass_input = self._page.locator("input[type='password']").first
            await pass_input.fill(password)

            # Human-like pause before clicking submit
            await self._page.wait_for_timeout(random.randint(800, 2000))

            # Click the sign-in/login button
            submit_btn = self._page.locator(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Sign In'), button:has-text('Log In')"
            ).first
            await submit_btn.click()

            # Wait for redirect to admin page
            import asyncio

            for _ in range(30):  # 30 seconds max
                await asyncio.sleep(1)
                if _is_admin_page(self._page.url):
                    logger.info("Auto-login successful")
                    console.print("[green]Auto-login successful![/green]")
                    return True

            logger.warning("Auto-login did not redirect to admin page")
            return False

        except Exception:
            logger.warning("Auto-login failed", exc_info=True)
            return False

    async def verify_logged_in(self) -> bool:
        """Verify that login succeeded by checking for seller portal content."""
        return _is_admin_page(self._page.url)
