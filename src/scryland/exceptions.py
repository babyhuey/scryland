"""Custom exception hierarchy for Scryland."""


class ScrylandError(Exception):
    """Base exception for all Scryland errors."""


class BrowserError(ScrylandError):
    """Browser or Playwright-related error."""


class LoginRequiredError(BrowserError):
    """Session expired or login needed."""


class NavigationError(BrowserError):
    """Page did not load as expected."""


class SelectorNotFoundError(BrowserError):
    """Expected DOM element not found."""


class PaginationIncompleteError(BrowserError):
    """A multi-page scrape could not reach all pages (timeout or stall).

    Raised so callers don't silently treat a partial result as complete.
    """


class PricingError(ScrylandError):
    """Pricing logic failure."""


class GuardrailError(PricingError):
    """Safety check blocked an action."""
