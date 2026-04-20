"""eBay OAuth flow.

eBay's user-scoped Sell APIs require an OAuth2 authorization-code grant.
Flow:
  1. Direct the user to the consent URL (scoped to the APIs we need).
  2. User signs in on eBay, approves, and is redirected to the RuName
     redirect URL. The browser shows a ?code=... query param we extract.
  3. Exchange the code for access + refresh tokens.
  4. Persist the refresh token (Fernet-encrypted).

Access tokens last ~2 hours; refresh tokens ~18 months. We refresh on
demand by exchanging the stored refresh token.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import httpx

from scryland.config import ScrylandConfig
from scryland.credentials import _derive_key  # reuse the project's PBKDF2 helper

logger = logging.getLogger("scryland")

_SCOPES = [
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
]

# The Browse API accepts an application (client_credentials) token, which
# does not require user consent. Used for competitor price lookups.
_APP_SCOPE = "https://api.ebay.com/oauth/api_scope"


def _endpoints(environment: str) -> tuple[str, str, str]:
    """Return (auth_url, token_url, api_base) for the given environment."""
    if environment == "sandbox":
        return (
            "https://auth.sandbox.ebay.com/oauth2/authorize",
            "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
            "https://api.sandbox.ebay.com",
        )
    return (
        "https://auth.ebay.com/oauth2/authorize",
        "https://api.ebay.com/identity/v1/oauth2/token",
        "https://api.ebay.com",
    )


@dataclass
class TokenBundle:
    access_token: str
    expires_at: float  # unix ts
    refresh_token: str


class EbayAuth:
    """Handles the one-time consent flow and refreshes on demand."""

    def __init__(self, config: ScrylandConfig) -> None:
        self._config = config
        self._auth_url, self._token_url, self._api_base = _endpoints(config.ebay_environment)
        self._cached: TokenBundle | None = None
        self._app_token: str | None = None
        self._app_token_expires_at: float = 0.0

    @property
    def api_base(self) -> str:
        return self._api_base

    def consent_url(self) -> str:
        """Return the URL the user must open in a browser to grant consent."""
        self._require("ebay_app_id", "ebay_redirect_uri_name")
        params = {
            "client_id": self._config.ebay_app_id,
            "response_type": "code",
            "redirect_uri": self._config.ebay_redirect_uri_name,
            "scope": " ".join(_SCOPES),
            "prompt": "login",
        }
        return f"{self._auth_url}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, passphrase: str) -> TokenBundle:
        """Exchange the consent-redirect `code` for tokens and persist them."""
        self._require("ebay_app_id", "ebay_cert_id", "ebay_redirect_uri_name")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                self._token_url,
                headers=self._basic_auth_headers(),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._config.ebay_redirect_uri_name,
                },
            )
        r.raise_for_status()
        bundle = self._parse_token_response(r.json())
        self._save(bundle, passphrase)
        self._cached = bundle
        return bundle

    async def access_token(self, passphrase: str) -> str:
        """Return a valid access token, refreshing from disk if needed."""
        if self._cached and self._cached.expires_at - 60 > time.time():
            return self._cached.access_token
        bundle = self._cached or self._load(passphrase)
        if bundle.expires_at - 60 > time.time():
            self._cached = bundle
            return bundle.access_token
        refreshed = await self._refresh(bundle.refresh_token)
        # Refresh response doesn't include a new refresh_token usually.
        bundle = TokenBundle(
            access_token=refreshed.access_token,
            expires_at=refreshed.expires_at,
            refresh_token=refreshed.refresh_token or bundle.refresh_token,
        )
        self._save(bundle, passphrase)
        self._cached = bundle
        return bundle.access_token

    async def app_access_token(self) -> str:
        """Return an application-scoped token (for Browse API etc.).

        Uses the OAuth2 client_credentials grant — no user consent needed,
        just the app credentials. Cached until near-expiry.
        """
        self._require("ebay_app_id", "ebay_cert_id")
        if self._app_token and self._app_token_expires_at - 60 > time.time():
            return self._app_token
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                self._token_url,
                headers=self._basic_auth_headers(),
                data={
                    "grant_type": "client_credentials",
                    "scope": _APP_SCOPE,
                },
            )
        r.raise_for_status()
        data = r.json()
        self._app_token = data["access_token"]
        self._app_token_expires_at = time.time() + int(data.get("expires_in", 0))
        return self._app_token

    async def _refresh(self, refresh_token: str) -> TokenBundle:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                self._token_url,
                headers=self._basic_auth_headers(),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": " ".join(_SCOPES),
                },
            )
        if r.status_code == 400:
            # `invalid_grant` = refresh token expired or revoked.
            # `invalid_request` = scopes changed since last auth.
            try:
                body = r.json()
            except ValueError:
                body = {}
            err = body.get("error", "")
            if err in ("invalid_grant", "invalid_request"):
                raise RuntimeError(
                    "eBay refresh token is no longer valid "
                    f"({err}). Run `scryland ebay-auth` to reconnect."
                )
        r.raise_for_status()
        return self._parse_token_response(r.json(), fallback_refresh=refresh_token)

    def _parse_token_response(
        self,
        data: dict,
        fallback_refresh: str | None = None,
    ) -> TokenBundle:
        return TokenBundle(
            access_token=data["access_token"],
            expires_at=time.time() + int(data.get("expires_in", 0)),
            refresh_token=data.get("refresh_token") or fallback_refresh or "",
        )

    def _basic_auth_headers(self) -> dict[str, str]:
        creds = f"{self._config.ebay_app_id}:{self._config.ebay_cert_id}"
        b = base64.b64encode(creds.encode()).decode()
        return {
            "Authorization": f"Basic {b}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    # ---- persistence (Fernet) ----

    def _path(self) -> Path:
        return Path(self._config.ebay_credentials_path)

    def _save(self, bundle: TokenBundle, passphrase: str) -> None:
        from cryptography.fernet import Fernet

        salt = b"scryland-ebay-salt"
        key = _derive_key(passphrase, salt)
        f = Fernet(key)
        payload = json.dumps(
            {
                "environment": self._config.ebay_environment,
                "access_token": bundle.access_token,
                "expires_at": bundle.expires_at,
                "refresh_token": bundle.refresh_token,
            }
        ).encode()
        encrypted = f.encrypt(payload)
        self._path().write_bytes(encrypted)
        self._path().chmod(0o600)

    def _load(self, passphrase: str) -> TokenBundle:
        from cryptography.fernet import Fernet, InvalidToken

        if not self._path().exists():
            raise RuntimeError("No eBay credentials found. Run `scryland ebay-auth` first.")
        salt = b"scryland-ebay-salt"
        key = _derive_key(passphrase, salt)
        try:
            payload = Fernet(key).decrypt(self._path().read_bytes())
        except InvalidToken as exc:
            raise RuntimeError("Wrong passphrase for eBay credentials") from exc
        data = json.loads(payload)
        return TokenBundle(
            access_token=data["access_token"],
            expires_at=float(data["expires_at"]),
            refresh_token=data["refresh_token"],
        )

    def _require(self, *field_names: str) -> None:
        missing = [f for f in field_names if not getattr(self._config, f)]
        if missing:
            raise RuntimeError(
                f"Missing eBay config: {', '.join(missing)}. Set via env vars "
                f"(SCRYLAND_EBAY_APP_ID, ...) or .env file."
            )
