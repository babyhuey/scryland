"""Configuration management using Pydantic settings."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ScrylandConfig(BaseSettings):
    """All operational configuration for Scryland.

    No credentials are stored here — login is manual.
    Values can be set via environment variables prefixed with SCRYLAND_
    or via a .env file.
    """

    model_config = SettingsConfigDict(env_prefix="SCRYLAND_", env_file=".env")

    # URLs
    seller_portal_url: str = "https://store.tcgplayer.com/admin/product/catalog"
    inventory_url: str = "https://store.tcgplayer.com/admin/product/catalog"
    login_url: str = "https://store.tcgplayer.com/admin/product/catalog"

    # Browser
    headless: bool = False
    browser_timeout_ms: int = 120_000  # 2 minutes
    slow_mo_ms: int = 100
    user_data_dir: str = ".scryland_session"

    # Pricing guardrails. A drop is treated as "big" only when BOTH the
    # percentage AND the absolute dollar amount exceed their thresholds —
    # that way a $0.04 → $0.03 move (25%, $0.01) goes through, but a
    # $5 → $0.50 crash (90%, $4.50) is blocked. Set max_price_change_abs
    # to 0 to fall back to pct-only behavior.
    max_price_change_pct: float = 10.0
    max_price_change_abs: float = 0.50
    min_price_floor: float = 0.25
    # Auto-default for "Apply this price change?" prompts after N seconds.
    # 0 = wait forever (default for one-shot commands). watch sets this
    # to a non-zero value so unattended runs progress instead of stalling.
    prompt_timeout_s: float = 0.0

    # Operation
    dry_run: bool = False
    page_size: int = 50
    max_retries: int = 3
    retry_delay_s: float = 2.0

    # Human-like behavior
    min_action_delay_ms: int = 500
    max_action_delay_ms: int = 2000
    no_delays: bool = False  # Skip all wait_for_timeout/human_delay calls

    # Database
    db_path: str = ".scryland_inventory.db"

    # Logging
    log_level: str = "INFO"
    log_file: str | None = None

    # eBay Sell API (set via environment or .env)
    ebay_environment: str = "production"  # or "sandbox"
    ebay_app_id: str = ""  # Client ID from developer.ebay.com
    ebay_cert_id: str = ""  # Client Secret
    ebay_dev_id: str = ""
    ebay_redirect_uri_name: str = ""  # RuName (not the URL) from keyset config
    ebay_fulfillment_policy_id: str = ""
    ebay_payment_policy_id: str = ""
    ebay_return_policy_id: str = ""
    ebay_merchant_location_key: str = "default"
    ebay_credentials_path: str = ".scryland_ebay_credentials"
    # Optional: set to skip the interactive passphrase prompt. Prefer keeping
    # this in a .env file (with 0600 perms) rather than shell history.
    ebay_passphrase: str = ""
    # Our buyer-visible shipping cost (matches the value we put on the
    # fulfillment policy). Used to compute undercut targets on a
    # total-price (item + shipping) basis so we actually beat competitors.
    ebay_shipping_cost: float = 0.99
    # Optional: your eBay seller username. If set, Scryland filters out
    # your own listings from the Browse undercut search (so you don't
    # compete with yourself). If unset, Scryland tries the /identity
    # endpoint — which requires the `commerce.identity.readonly` OAuth
    # scope. Setting this manually here avoids needing that scope.
    ebay_seller_username: str = ""

    @property
    def user_data_path(self) -> Path:
        return Path(self.user_data_dir).resolve()
