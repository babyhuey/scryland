"""Minimal eBay Sell API client wrapping the Inventory API endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from scryland.config import ScrylandConfig
from scryland.ebay.auth import EbayAuth
from scryland.ebay.listing import EbayListing

logger = logging.getLogger("scryland")


_DEFAULT_POLICY_NAMES = {
    "fulfillment": "Scryland — Standard Envelope",
    "payment": "Scryland — Managed Payments",
    "return": "Scryland — 30 Day Returns",
}


# Default shipping. In production, for trading cards the cheapest option is
# `eBayStandardEnvelope` ($0.70, tracked, up to $20 value). Sandbox doesn't
# support that service code; `USPSPriority` is safer for sandbox testing.
def _fulfillment_policy(service_code: str, cost: str) -> dict:
    return {
        "name": _DEFAULT_POLICY_NAMES["fulfillment"],
        "marketplaceId": "EBAY_US",
        "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
        "handlingTime": {"value": 1, "unit": "DAY"},
        "shippingOptions": [
            {
                "optionType": "DOMESTIC",
                "costType": "FLAT_RATE",
                "shippingServices": [
                    {
                        "sortOrder": 1,
                        "shippingCarrierCode": "USPS",
                        "shippingServiceCode": service_code,
                        "shippingCost": {"value": cost, "currency": "USD"},
                        "additionalShippingCost": {"value": "0.00", "currency": "USD"},
                        "freeShipping": False,
                        "buyerResponsibleForShipping": False,
                    }
                ],
            }
        ],
    }


_DEFAULT_PAYMENT_POLICY = {
    "name": _DEFAULT_POLICY_NAMES["payment"],
    "marketplaceId": "EBAY_US",
    "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
    "immediatePay": True,
}

_DEFAULT_RETURN_POLICY = {
    "name": _DEFAULT_POLICY_NAMES["return"],
    "marketplaceId": "EBAY_US",
    "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
    "returnsAccepted": True,
    "returnShippingCostPayer": "BUYER",
    "returnMethod": "MONEY_BACK",
    "returnPeriod": {"value": 30, "unit": "DAY"},
}


@dataclass
class PublishResult:
    sku: str
    offer_id: str | None
    listing_id: str | None  # populated when published
    draft: bool
    warnings: list[str]


class EbayClient:
    def __init__(self, config: ScrylandConfig, auth: EbayAuth, passphrase: str) -> None:
        import asyncio

        self._config = config
        self._auth = auth
        self._passphrase = passphrase
        self._http = httpx.AsyncClient(
            base_url=auth.api_base,
            timeout=30.0,
            event_hooks={"request": [self._rate_limit_hook]},
        )
        self._own_seller_username: str | None = None
        self._seller_lock = asyncio.Lock()
        # Token-bucket rate limiter: eBay's Sell APIs generally tolerate
        # a few hundred calls/min. We sleep here when we exceed our budget
        # to keep us well under any published limits.
        self._rate_lock = asyncio.Lock()
        self._rate_calls: list[float] = []  # timestamps of recent calls
        self._rate_limit_per_min = 300

    async def _rate_limit_hook(self, request) -> None:
        """Sleep before sending if we've hit the per-minute call budget."""
        import time as _time

        async with self._rate_lock:
            now = _time.monotonic()
            # Drop calls older than 60s.
            cutoff = now - 60.0
            self._rate_calls = [t for t in self._rate_calls if t > cutoff]
            if len(self._rate_calls) >= self._rate_limit_per_min:
                # Wait until the oldest call exits the 60s window.
                sleep_for = 60.0 - (now - self._rate_calls[0]) + 0.05
                if sleep_for > 0:
                    logger.debug(
                        "eBay rate limit: sleeping %.2fs (budget %d/min)",
                        sleep_for,
                        self._rate_limit_per_min,
                    )
                    await asyncio.sleep(sleep_for)
                now = _time.monotonic()
                cutoff = now - 60.0
                self._rate_calls = [t for t in self._rate_calls if t > cutoff]
            self._rate_calls.append(now)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> EbayClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def update_fulfillment_shipping_cost(
        self,
        policy_id: str,
        shipping_cost: str,
        *,
        additional_cost: str = "0.00",
    ) -> bool:
        """Update an existing fulfillment policy's domestic shipping price."""
        g = await self._http.get(
            f"/sell/account/v1/fulfillment_policy/{policy_id}",
            headers=await self._headers(content_json=False),
        )
        if g.status_code >= 300:
            logger.warning(
                "get fulfillment policy %s failed %d: %s",
                policy_id,
                g.status_code,
                g.text[:300],
            )
            return False
        body = g.json()
        # Strip fields eBay rejects on PUT
        for k in ("fulfillmentPolicyId",):
            body.pop(k, None)
        for opt in body.get("shippingOptions") or []:
            if opt.get("optionType") != "DOMESTIC":
                continue
            for svc in opt.get("shippingServices") or []:
                svc["shippingCost"] = {"value": shipping_cost, "currency": "USD"}
                svc["additionalShippingCost"] = {"value": additional_cost, "currency": "USD"}
        r = await self._http.put(
            f"/sell/account/v1/fulfillment_policy/{policy_id}",
            headers=await self._headers(),
            json=body,
        )
        if r.status_code >= 300:
            # eBay returns 400 when the policy is already at the requested
            # value ("Business Profile information in the request is the
            # same as in the system"). Treat as success — nothing to do.
            if b"same as in the system" in r.content.lower():
                logger.info(
                    "Fulfillment policy %s already at requested shipping cost",
                    policy_id,
                )
                return True
            logger.warning(
                "update fulfillment policy %s failed %d: %s",
                policy_id,
                r.status_code,
                r.text[:300],
            )
            return False
        return True

    async def get_own_seller_username(
        self,
        sample_listing_id: str | None = None,
    ) -> str | None:
        """Return our eBay seller username.

        Sources in priority order:
          1. `config.ebay_seller_username` (set in .env) — preferred.
          2. GET /commerce/identity/v1/user — requires the
             commerce.identity.readonly scope. Many users don't have it.
          3. GET /buy/browse/v1/item/v1|{listing_id}|0 using one of our own
             listing IDs — the `seller.username` field is in the response.
             Uses only the app token we already have. Works as long as at
             least one of our listings is live on eBay.

        The lock makes this safe to call concurrently from a gather()
        loop — only the first task will hit the API; the rest wait.
        """
        async with self._seller_lock:
            if self._own_seller_username:
                return self._own_seller_username
            if self._own_seller_username == "":
                return None
            configured = getattr(self._config, "ebay_seller_username", "") or ""
            if configured:
                self._own_seller_username = configured
                return configured
            # Attempt #1: /identity (needs extra scope many users lack).
            try:
                r = await self._http.get(
                    "/commerce/identity/v1/user",
                    headers=await self._headers(content_json=False),
                )
                if r.status_code < 300:
                    name = (r.json() or {}).get("username") or ""
                    if name:
                        self._own_seller_username = name
                        return name
            except Exception:
                logger.debug("identity endpoint errored", exc_info=True)
            # Attempt #2: Browse API on one of our own listings. Any live
            # listing carries our seller.username in the response.
            if sample_listing_id:
                try:
                    token = await self._auth.app_access_token()
                    item_id = f"v1|{sample_listing_id}|0"
                    br = await self._http.get(
                        f"/buy/browse/v1/item/{item_id}",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                            "Accept": "application/json",
                        },
                    )
                    if br.status_code < 300:
                        name = (br.json().get("seller") or {}).get("username") or ""
                        if name:
                            logger.info(
                                "Resolved eBay seller username via Browse: %s",
                                name,
                            )
                            self._own_seller_username = name
                            return name
                    else:
                        logger.debug(
                            "Browse item lookup for username failed %d: %s",
                            br.status_code,
                            br.text[:200],
                        )
                except Exception:
                    logger.debug(
                        "Browse item lookup for username errored",
                        exc_info=True,
                    )
            logger.warning(
                "Couldn't resolve eBay seller username — self-seller filter "
                "disabled. Set SCRYLAND_EBAY_SELLER_USERNAME in .env to fix."
            )
            self._own_seller_username = ""  # sentinel
            return None

    async def iter_offers_for_skus(
        self,
        skus: list[str],
        *,
        marketplace_id: str = "EBAY_US",
        concurrency: int = 8,
    ) -> list[dict]:
        """Fetch offers for each SKU in `skus`, return them flat.

        eBay's Sell Inventory API has no bulk "list all my offers" endpoint
        — `/sell/inventory/v1/offer` requires the `sku` query parameter
        (errorId 25707 if you omit it). So sync-inventory enumerates SKUs
        from our local DB and GETs per-SKU, with bounded concurrency.

        SKUs whose lookup 404s or returns no offers are silently dropped —
        they were ended/withdrawn outside scryland and are no longer live.
        """
        sem = asyncio.Semaphore(concurrency)

        async def _fetch(sku: str) -> list[dict]:
            async with sem:
                r = await self._http.get(
                    "/sell/inventory/v1/offer",
                    params={
                        "sku": sku,
                        "marketplace_id": marketplace_id,
                        "format": "FIXED_PRICE",
                        "limit": 100,
                    },
                    headers=await self._headers(content_json=False),
                )
            if r.status_code == 404:
                return []
            if r.status_code >= 300:
                logger.warning(
                    "list offers for sku %s failed %d: %s",
                    sku,
                    r.status_code,
                    r.text[:200],
                )
                return []
            return r.json().get("offers") or []

        results = await asyncio.gather(*(_fetch(sku) for sku in skus))
        flat: list[dict] = []
        for batch in results:
            flat.extend(batch)
        return flat

    async def get_inventory_item(self, sku: str) -> dict | None:
        """Fetch an inventory item by SKU.

        Returns None only for a true 404 (doesn't exist). Other errors
        raise so the caller can't accidentally mistake them for "absent"
        and create duplicates.
        """
        r = await self._http.get(
            f"/sell/inventory/v1/inventory_item/{sku}",
            headers=await self._headers(content_json=False),
        )
        if r.status_code == 404:
            return None
        if r.status_code >= 300:
            raise RuntimeError(f"get inventory_item {sku} failed {r.status_code}: {r.text[:300]}")
        return r.json()

    async def opt_in_to_business_policies(self) -> None:
        """Opt the seller in to the Business Policies program (idempotent).

        Sandbox / new accounts often aren't opted in by default, which makes
        every fulfillment/payment/return call fail with errorId 20403. This
        hits `/sell/account/v1/program/opt_in` to flip the flag.
        """
        r = await self._http.post(
            "/sell/account/v1/program/opt_in",
            headers=await self._headers(),
            json={"programType": "SELLING_POLICY_MANAGEMENT"},
        )
        if r.status_code == 204:
            logger.info("Opted in to Business Policies")
            return
        if r.status_code == 409:
            logger.debug("Already opted in to Business Policies")
            return
        if r.status_code >= 300:
            # Don't hard-fail — some accounts are auto-opted and report odd
            # errors here. We'll surface downstream errors instead.
            logger.warning("opt-in returned %d: %s", r.status_code, r.text[:300])

    async def create_default_policies(
        self,
        shipping_service: str | None = None,
        shipping_cost: str | None = None,
    ) -> dict[str, str]:
        """Create sensible default fulfillment/payment/return policies.

        Idempotent-ish: if a policy with the same `name` already exists,
        eBay returns 400 "already exists" — we swallow that and look it up.
        Returns {"fulfillment": id, "payment": id, "return": id}.
        """
        await self.opt_in_to_business_policies()
        existing = await self.list_business_policies()

        # Shipping service defaults per environment. eBay Standard Envelope
        # would be cheapest ($0.71 for cards <$20) but requires account-level
        # enrollment (LSAS eligibility). Until enrolled, fall back to Ground
        # Advantage which every USPS-enabled seller can use.
        is_sandbox = "sandbox" in self._http.base_url.host.lower()
        # Production default: eBay Standard Envelope — tracked, ~$0.71 for
        # trading cards under $20. Note the required "US_" prefix.
        svc = shipping_service or ("USPSPriority" if is_sandbox else "US_eBayStandardEnvelope")
        cost = shipping_cost or ("4.99" if is_sandbox else "0.71")
        fulfillment_body = _fulfillment_policy(svc, cost)
        existing_ids = {
            kind: next(
                (p["id"] for p in items if p["name"] == _DEFAULT_POLICY_NAMES[kind]),
                None,
            )
            for kind, items in existing.items()
        }

        async def upsert(kind: str, path: str, body: dict, id_field: str) -> str:
            if existing_ids.get(kind):
                logger.info("%s policy '%s' already exists", kind, body["name"])
                return existing_ids[kind]
            r = await self._http.post(
                path,
                headers=await self._headers(),
                json=body,
            )
            if r.status_code >= 300:
                raise RuntimeError(f"create {kind} policy failed {r.status_code}: {r.text[:500]}")
            return r.json()[id_field]

        fulfillment_id = await upsert(
            "fulfillment",
            "/sell/account/v1/fulfillment_policy",
            fulfillment_body,
            "fulfillmentPolicyId",
        )
        payment_id = await upsert(
            "payment",
            "/sell/account/v1/payment_policy",
            _DEFAULT_PAYMENT_POLICY,
            "paymentPolicyId",
        )
        return_id = await upsert(
            "return",
            "/sell/account/v1/return_policy",
            _DEFAULT_RETURN_POLICY,
            "returnPolicyId",
        )
        return {
            "fulfillment": fulfillment_id,
            "payment": payment_id,
            "return": return_id,
        }

    async def list_business_policies(self) -> dict[str, list[dict]]:
        """Return (fulfillment, payment, return) policy IDs for the account."""
        out: dict[str, list[dict]] = {}
        for kind, path in (
            ("fulfillment", "/sell/account/v1/fulfillment_policy"),
            ("payment", "/sell/account/v1/payment_policy"),
            ("return", "/sell/account/v1/return_policy"),
        ):
            r = await self._http.get(
                path,
                headers=await self._headers(content_json=False),
                params={"marketplace_id": "EBAY_US"},
            )
            if r.status_code >= 300:
                logger.warning(
                    "list %s_policy failed %d: %s",
                    kind,
                    r.status_code,
                    r.text[:300],
                )
                out[kind] = []
                continue
            body = r.json()
            items = body.get(f"{kind}Policies") or []
            out[kind] = [{"id": p.get(f"{kind}PolicyId"), "name": p.get("name")} for p in items]
        return out

    async def create_merchant_location(
        self,
        key: str,
        *,
        country: str,
        city: str,
        state: str,
        postal_code: str,
        address_line1: str | None = None,
    ) -> None:
        """Create (or upsert) an inventory location. Required before listing."""
        body = {
            "location": {
                "address": {
                    "country": country,
                    "city": city,
                    "stateOrProvince": state,
                    "postalCode": postal_code,
                    **({"addressLine1": address_line1} if address_line1 else {}),
                },
            },
            "name": key,
            "merchantLocationStatus": "ENABLED",
            "locationTypes": ["WAREHOUSE"],
        }
        r = await self._http.post(
            f"/sell/inventory/v1/location/{key}",
            headers=await self._headers(),
            json=body,
        )
        if r.status_code == 409:
            # already exists — treat as success
            logger.info("Merchant location '%s' already exists", key)
            return
        if r.status_code >= 300:
            raise RuntimeError(f"create location failed {r.status_code}: {r.text[:500]}")

    async def find_lowest_price(
        self,
        card_name: str,
        set_name: str,
        collector_number: str,
        is_foil: bool,
        *,
        condition: str | None = None,
        include_foil: bool | None = None,
    ) -> float | None:
        """Search eBay for matching listings and return the lowest total
        price (price + shipping), or None if no matches.

        Uses the Browse API with an application token. Matching is stricter
        than before:
        - Condition filter: ungraded only, ≥ condition supplied (NM → only
          NM; LP → LP or better; etc.). Prevents a Damaged competitor from
          pulling our NM price down.
        - Set match: title must contain the set name (normalized) or be
          within the same category — avoids reprints of same name polluting.
        - Foil vs non-foil: when we know the finish, results whose title
          says "foil" are excluded from non-foil searches and vice versa.
        - Shipping: takes the cheapest shipping option per listing, not the
          first (eBay lists expedited first sometimes).
        """
        token = await self._auth.app_access_token()
        finish_is_foil = is_foil if include_foil is None else include_foil

        # Query string: front face of name only (other sellers usually
        # list DFCs by front face alone, not the full "A // B" form), +
        # set name + collector. Don't include "foil" as a keyword — it's
        # noisy. We filter by title below.
        front_name = card_name.split("//")[0].strip()
        q_parts = [front_name]
        if set_name:
            q_parts.append(set_name.split(":")[0])  # main set, not subset
        if collector_number:
            q_parts.append(collector_number.lstrip("0") or "0")
        q = " ".join(q_parts)

        # No condition filter — eBay's `conditionIds` filter EXCLUDES
        # any listing whose conditionId is unset or using an older code,
        # which covers a lot of older listings. We keep title/name/set
        # filters which do most of the work anyway. If Damaged listings
        # start undercutting us in practice we can bolt a filter back on.
        filters = [
            "buyingOptions:{FIXED_PRICE}",
            "itemLocationCountry:US",
        ]
        params = {
            "q": q,
            "limit": "50",
            "sort": "price",
            "filter": ",".join(filters),
            # NOTE: no category_ids filter — eBay's Browse can be overly
            # strict about the exact category, causing listings in MTG
            # sub-leaves (e.g. "Magic: The Gathering Individual Trading
            # Card Games > Uncommon") to be excluded even though they
            # live under 183454. The name/set/foil title filters below
            # are enough to keep noise out.
        }
        r = await self._http.get(
            "/buy/browse/v1/item_summary/search",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                "Accept": "application/json",
            },
            params=params,
        )
        if r.status_code >= 300:
            # Callers already wrap this in try/except and bump a
            # browse_errors counter. Raising here keeps "search failed"
            # distinct from "search returned zero results" (None).
            raise RuntimeError(f"Browse search {r.status_code}: {r.text[:300]}")
        body = r.json()
        items = body.get("itemSummaries") or []
        logger.debug(
            "Browse '%s': %d raw results (total %s)",
            q,
            len(items),
            body.get("total"),
        )

        # Exclude lots, playsets, proxies, and obviously bundled listings.
        bad_terms = (
            "playset",
            "lot of",
            "bundle",
            "proxy",
            "proxies",
            "custom",
            "lot ",
            "4x ",
            "2x ",
            "3x ",
        )

        def total_price(it: dict) -> float | None:
            try:
                base = float((it.get("price") or {}).get("value") or 0)
            except (TypeError, ValueError):
                return None
            # Take the CHEAPEST shipping option, not the first. Listings
            # sometimes list expedited first.
            cheapest_ship: float | None = None
            for opt in it.get("shippingOptions") or []:
                try:
                    s = float((opt.get("shippingCost") or {}).get("value") or 0)
                    if cheapest_ship is None or s < cheapest_ship:
                        cheapest_ship = s
                except (TypeError, ValueError):
                    continue
            return base + (cheapest_ship or 0.0)

        name_token = card_name.lower().split("//")[0].strip()
        set_token = (set_name or "").lower().split(":")[0].strip()
        own_seller = await self.get_own_seller_username()

        # Rejection counters for visibility.
        rej = {
            "bad_term": 0,
            "name_miss": 0,
            "foil_mismatch": 0,
            "set_miss": 0,
            "self": 0,
            "no_price": 0,
        }

        candidates: list[float] = []
        cheapest_title = None
        cheapest_total = None
        for it in items:
            title = (it.get("title") or "").lower()
            if any(t in title for t in bad_terms):
                rej["bad_term"] += 1
                continue
            if name_token not in title:
                rej["name_miss"] += 1
                continue
            # Finish match: listings with "foil" in title when we want
            # non-foil, or no-foil in title when we want foil. Strip
            # "non[- ]?foil"/"nonfoil" first so a properly-tagged non-foil
            # listing isn't rejected by the substring "foil".
            scrubbed = re.sub(r"\bnon[\s-]?foil\b", "", title)
            title_has_foil = bool(re.search(r"\bfoil\b|\betched\b", scrubbed))
            if finish_is_foil and not title_has_foil:
                rej["foil_mismatch"] += 1
                continue
            if not finish_is_foil and title_has_foil:
                rej["foil_mismatch"] += 1
                continue
            # Set match: relaxed so we don't exclude listings where the
            # seller used an abbreviation ("SOA" / "SOS" / "STX") instead
            # of the full set name. We require at least one significant
            # word from the set in the title, skipping if the set name
            # is too short to be distinctive.
            if set_token and len(set_token) >= 4:
                set_words = [
                    w
                    for w in set_token.split()
                    if len(w) >= 4 and w not in ("the", "and", "of", "for")
                ]
                if set_words and not any(w in title for w in set_words):
                    rej["set_miss"] += 1
                    continue
            # Skip our own listings — we should not undercut ourselves.
            if own_seller:
                seller = (it.get("seller") or {}).get("username") or ""
                if seller.lower() == own_seller.lower():
                    rej["self"] += 1
                    continue
            total = total_price(it)
            if total is None or total <= 0:
                rej["no_price"] += 1
                continue
            candidates.append(total)
            if cheapest_total is None or total < cheapest_total:
                cheapest_total = total
                cheapest_title = it.get("title", "")

        logger.debug(
            "Browse '%s': %d kept, rej=%s, cheapest=%s @ $%s",
            q,
            len(candidates),
            rej,
            (cheapest_title or "")[:60],
            cheapest_total,
        )

        # If Browse returned results but all got filtered out, that's
        # notable — either the matcher is overly strict or the set has
        # Demoted to debug — these fire on legit cases (foil-only sets,
        # sole-listing self-rejections) and just clutter the watch log.
        # The samples-and-rej payload is still useful when investigating
        # "why didn't this card get repriced?" via --log-level debug.
        if items and not candidates:
            samples = [(it.get("title") or "")[:80] for it in items[:2]]
            logger.debug(
                "Browse '%s': %d raw results, all filtered out (rej=%s, samples=%s)",
                q,
                len(items),
                rej,
                samples,
            )

        if not candidates:
            return None
        return min(candidates)

    async def withdraw_offer(self, offer_id: str) -> bool:
        """End a published listing. Returns True on success or already-ended.

        Uses POST /sell/inventory/v1/offer/{offerId}/withdraw which ends the
        listing but preserves the offer so it can be republished later.
        """
        r = await self._http.post(
            f"/sell/inventory/v1/offer/{offer_id}/withdraw",
            headers=await self._headers(),
        )
        if r.status_code == 204 or r.status_code < 300:
            return True
        # Parse the response and check both known "already inactive" error
        # ids AND a message substring fallback (eBay's error taxonomy
        # shifts — logging benign hits at INFO so we can update this list
        # when new ones appear).
        try:
            body = r.json()
        except ValueError:
            body = {}
        # 25001 = offer is not published; other 25xxx codes vary.
        benign_ids = {25001}
        errors = body.get("errors") or []
        for e in errors:
            eid = int(e.get("errorId") or 0)
            msg = (e.get("message") or "").lower()
            long_msg = (e.get("longMessage") or "").lower()
            if eid in benign_ids or any(
                phrase in msg or phrase in long_msg
                for phrase in (
                    "not published",
                    "already withdrawn",
                    "already ended",
                    "listing is not active",
                )
            ):
                logger.info(
                    "withdraw offer %s — treating errorId %s as already-ended",
                    offer_id,
                    eid,
                )
                return True
        logger.warning("withdraw offer %s failed %d: %s", offer_id, r.status_code, r.text[:300])
        return False

    async def update_offer_price(
        self,
        offer_id: str,
        price_usd: float,
        quantity: int | None = None,
    ) -> bool:
        """Patch an existing offer's price (and optionally quantity)."""
        # Fetch current offer → merge → PUT (full replace required).
        g = await self._http.get(
            f"/sell/inventory/v1/offer/{offer_id}",
            headers=await self._headers(content_json=False),
        )
        if g.status_code >= 300:
            logger.warning("get offer %s failed %d: %s", offer_id, g.status_code, g.text[:300])
            return False
        body = g.json()
        body["pricingSummary"] = {
            "price": {"value": f"{price_usd:.2f}", "currency": "USD"},
        }
        if quantity is not None:
            body["availableQuantity"] = quantity
        # Strip read-only fields that PUT rejects.
        for key in ("offerId", "status", "listing", "listingId"):
            body.pop(key, None)

        import asyncio

        # Retry 5xx (eBay's "System error. Please try again later.") a few
        # times with backoff before giving up — same pattern as publish.
        attempts = 4
        backoff = 2.0
        for attempt in range(1, attempts + 1):
            r = await self._http.put(
                f"/sell/inventory/v1/offer/{offer_id}",
                headers=await self._headers(),
                json=body,
            )
            if r.status_code < 300:
                # Verify the update actually persisted. eBay occasionally
                # ignores a price change with warnings (not 4xx). GET the
                # offer back and confirm.
                g = await self._http.get(
                    f"/sell/inventory/v1/offer/{offer_id}",
                    headers=await self._headers(content_json=False),
                )
                if g.status_code < 300:
                    got_body = g.json()
                    got = (got_body.get("pricingSummary") or {}).get("price", {})
                    try:
                        actual = float(got.get("value") or 0)
                    except (TypeError, ValueError):
                        actual = 0.0
                    price_ok = abs(actual - price_usd) < 0.005
                    # Also verify qty if caller specified one.
                    qty_ok = True
                    if quantity is not None:
                        actual_qty = int(got_body.get("availableQuantity") or 0)
                        qty_ok = actual_qty == quantity
                        if not qty_ok:
                            logger.warning(
                                "update offer %s quantity did not persist "
                                "(requested %d, actual %d)",
                                offer_id,
                                quantity,
                                actual_qty,
                            )
                    if price_ok and qty_ok:
                        return True
                    if not price_ok:
                        logger.warning(
                            "update offer %s PUT 200 but price did not persist "
                            "(requested $%.2f, actual $%.2f)",
                            offer_id,
                            price_usd,
                            actual,
                        )
                    return False
                # Verify GET failed — we can't confirm persistence, so
                # DON'T claim success. DB keeps the old price, next watch
                # run will retry.
                logger.warning(
                    "update offer %s PUT ok but verify GET failed %d — "
                    "returning False so DB stays in sync with unknown state",
                    offer_id,
                    g.status_code,
                )
                return False
            is_retryable = r.status_code >= 500 or r.status_code == 429
            if not is_retryable or attempt == attempts:
                logger.warning(
                    "update offer %s price failed %d: %s",
                    offer_id,
                    r.status_code,
                    r.text[:300],
                )
                return False
            wait_s = backoff
            if r.status_code == 429:
                try:
                    wait_s = max(wait_s, float(r.headers.get("Retry-After", "0")))
                except ValueError:
                    pass
            logger.warning(
                "update offer %s attempt %d/%d hit %d — retrying in %.1fs",
                offer_id,
                attempt,
                attempts,
                r.status_code,
                wait_s,
            )
            await asyncio.sleep(wait_s)
            backoff *= 2
        return False

    async def publish_listing(
        self,
        listing: EbayListing,
        *,
        draft: bool = False,
    ) -> PublishResult:
        """Create/update inventory item, create offer, and publish.

        In draft mode, the publish step is skipped and the caller can
        publish via Seller Hub after inspection.
        """
        warnings: list[str] = []

        await self._put_inventory_item(listing, warnings)
        offer_id = await self._create_offer(listing, warnings)

        listing_id: str | None = None
        if not draft:
            listing_id = await self._publish_offer(offer_id, warnings)

        return PublishResult(
            sku=listing.sku,
            offer_id=offer_id,
            listing_id=listing_id,
            draft=draft,
            warnings=warnings,
        )

    async def _headers(self, *, content_json: bool = True) -> dict[str, str]:
        token = await self._auth.access_token(self._passphrase)
        h = {
            "Authorization": f"Bearer {token}",
            "Content-Language": "en-US",
            "Accept": "application/json",
        }
        if content_json:
            h["Content-Type"] = "application/json"
        return h

    async def _put_inventory_item(self, listing: EbayListing, warnings: list[str]) -> None:
        body = {
            "availability": {
                "shipToLocationAvailability": {"quantity": listing.quantity},
            },
            "condition": _condition_id_to_enum(listing.condition_id),
            "conditionDescriptors": listing.condition_descriptors,
            "product": {
                "title": listing.title,
                "description": listing.description_html,
                "aspects": listing.aspects,
                "imageUrls": listing.image_urls,
            },
        }
        # conditionDescription is rejected for CCG/TCG categories; only send
        # it for other categories where free-text description is allowed.
        if listing.category_id not in {"183454", "261328", "183050"}:
            body["conditionDescription"] = listing.condition_description

        # Retry on 5xx and on errorId 25001 ("Internal Server Error" /
        # "Core Inventory Service internal error") which eBay returns
        # with status 400 OR 500 transiently.
        attempts = 4
        backoff_s = 2.0
        for attempt in range(1, attempts + 1):
            r = await self._http.put(
                f"/sell/inventory/v1/inventory_item/{listing.sku}",
                headers=await self._headers(),
                json=body,
            )
            _extract_warnings(r, warnings)
            if r.status_code < 300:
                break
            body_lower = r.text.lower() if r.text else ""
            # \b prevents 25001 from matching 250010 etc. if eBay ever
            # widens its error-ID space.
            is_eventual_error = (
                bool(re.search(r'"errorid":\s*25001\b', body_lower))
                or "core inventory service internal error" in body_lower
            )
            is_retryable = r.status_code >= 500 or r.status_code == 429 or is_eventual_error
            if not is_retryable or attempt == attempts:
                raise RuntimeError(f"PUT inventory_item failed {r.status_code}: {r.text[:500]}")
            logger.warning(
                "PUT inventory_item attempt %d/%d hit %d — retrying in %.1fs",
                attempt,
                attempts,
                r.status_code,
                backoff_s,
            )
            await asyncio.sleep(backoff_s)
            backoff_s *= 2

        # If eBay flagged any aspect rename, GET the item back and diff the
        # aspect keys so we can see exactly which name(s) got rewritten.
        # This converts the opaque "Some item specifics were renamed"
        # warning into an actionable mapping like {"Set": "Set Name"}.
        if any("renamed" in w.lower() for w in warnings):
            try:
                g = await self._http.get(
                    f"/sell/inventory/v1/inventory_item/{listing.sku}",
                    headers=await self._headers(content_json=False),
                )
                if g.status_code < 300:
                    stored = (g.json().get("product") or {}).get("aspects") or {}
                    sent_keys = set(listing.aspects.keys())
                    stored_keys = set(stored.keys())
                    dropped = sent_keys - stored_keys
                    added = stored_keys - sent_keys
                    if dropped or added:
                        logger.warning(
                            "eBay aspect rename detected — sent but missing: %s; "
                            "appeared after rename: %s",
                            sorted(dropped),
                            sorted(added),
                        )
            except Exception:
                logger.debug("Could not GET inventory item to diff aspects", exc_info=True)

    async def _create_offer(self, listing: EbayListing, warnings: list[str]) -> str:
        body = {
            "sku": listing.sku,
            "marketplaceId": "EBAY_US",
            "format": "FIXED_PRICE",
            "availableQuantity": listing.quantity,
            "categoryId": listing.category_id,
            "listingDescription": listing.description_html,
            "pricingSummary": {
                "price": {
                    "value": f"{listing.price_usd:.2f}",
                    "currency": "USD",
                },
            },
            "merchantLocationKey": self._config.ebay_merchant_location_key,
            "listingPolicies": {
                "fulfillmentPolicyId": self._config.ebay_fulfillment_policy_id,
                "paymentPolicyId": self._config.ebay_payment_policy_id,
                "returnPolicyId": self._config.ebay_return_policy_id,
            },
        }
        # Look up an existing offer for this SKU — eBay disallows two.
        existing_id = await self._find_offer_for_sku(listing.sku)
        if existing_id:
            r = await self._http.put(
                f"/sell/inventory/v1/offer/{existing_id}",
                headers=await self._headers(),
                json=body,
            )
            _extract_warnings(r, warnings)
            if r.status_code >= 300:
                raise RuntimeError(
                    f"PUT offer {existing_id} failed {r.status_code}: {r.text[:500]}"
                )
            return existing_id

        r = await self._http.post(
            "/sell/inventory/v1/offer",
            headers=await self._headers(),
            json=body,
        )
        _extract_warnings(r, warnings)
        if r.status_code >= 300:
            raise RuntimeError(f"POST offer failed {r.status_code}: {r.text[:500]}")
        offer_id = r.json().get("offerId")
        if not offer_id:
            raise RuntimeError(f"POST offer returned no offerId: {r.text[:500]}")
        return offer_id

    async def _find_offer_for_sku(self, sku: str) -> str | None:
        """Return the offerId of an existing offer for this SKU, or None."""
        r = await self._http.get(
            "/sell/inventory/v1/offer",
            headers=await self._headers(content_json=False),
            params={"sku": sku},
        )
        if r.status_code == 404:
            return None
        if r.status_code >= 300:
            logger.warning("lookup offer by sku %s: %d %s", sku, r.status_code, r.text[:200])
            return None
        offers = r.json().get("offers") or []
        if not offers:
            return None
        return offers[0].get("offerId")

    async def _publish_offer(self, offer_id: str, warnings: list[str]) -> str:
        import asyncio

        # Sandbox regularly 500s on publish with "try again later" — and even
        # production occasionally does. Retry with backoff before giving up.
        attempts = 4
        backoff_s = 2.0
        last_status = 0
        last_text = ""
        for attempt in range(1, attempts + 1):
            r = await self._http.post(
                f"/sell/inventory/v1/offer/{offer_id}/publish",
                headers=await self._headers(),
            )
            _extract_warnings(r, warnings)
            if r.status_code < 300:
                return r.json().get("listingId", "")

            # Already published → treat as success, fetch listingId.
            if b"already published" in r.content.lower():
                logger.info("Offer %s was already published", offer_id)
                g = await self._http.get(
                    f"/sell/inventory/v1/offer/{offer_id}",
                    headers=await self._headers(content_json=False),
                )
                if g.status_code < 300:
                    listing_id = g.json().get("listing", {}).get("listingId", "")
                    if listing_id:
                        return listing_id
                raise RuntimeError(
                    f"Offer {offer_id} already published but could not "
                    f"retrieve listingId (GET returned {g.status_code})"
                )

            last_status, last_text = r.status_code, r.text[:500]
            # Retry transient errors (5xx) and rate-limits (429). Also retry
            # 25604 "Availability not found" — eBay's inventory service is
            # eventually-consistent, so the availability data from our PUT
            # sometimes hasn't propagated to publish yet.
            body_lower = r.text.lower() if r.text else ""
            is_eventual_consistency = r.status_code == 400 and (
                bool(re.search(r'"errorid":\s*25604\b', body_lower))
                or "availability not found" in body_lower
            )
            is_retryable = r.status_code >= 500 or r.status_code == 429 or is_eventual_consistency
            if not is_retryable or attempt == attempts:
                break
            wait_s = backoff_s
            if r.status_code == 429:
                # Honor Retry-After when present.
                try:
                    wait_s = max(wait_s, float(r.headers.get("Retry-After", "0")))
                except ValueError:
                    pass
            logger.warning(
                "publish offer %s attempt %d/%d hit %d — retrying in %.1fs",
                offer_id,
                attempt,
                attempts,
                r.status_code,
                wait_s,
            )
            await asyncio.sleep(wait_s)
            backoff_s *= 2

        raise RuntimeError(f"publish offer failed {last_status}: {last_text}")


# Known benign warnings that eBay attaches to nearly every call — not
# actionable, not a per-listing problem. We log them at DEBUG once per
# process run instead of fanning them out to every listing.
_SUPPRESSED_WARNING_SUBSTRINGS = (
    "Funds from your sales may be unavailable",
    "You must accept these terms and conditions for automatic payment",
    "Seller has opted into business policies",
)
_LOGGED_SUPPRESSED: set[str] = set()


def _extract_warnings(response: httpx.Response, warnings: list[str]) -> None:
    try:
        body = response.json()
    except ValueError:
        return
    for w in body.get("warnings", []) or []:
        msg = w.get("message") or str(w)
        if any(s in msg for s in _SUPPRESSED_WARNING_SUBSTRINGS):
            key = next(s for s in _SUPPRESSED_WARNING_SUBSTRINGS if s in msg)
            if key not in _LOGGED_SUPPRESSED:
                _LOGGED_SUPPRESSED.add(key)
                logger.debug("eBay benign warning (suppressed further): %s", msg)
            continue
        warnings.append(msg)
        logger.warning("eBay API warning: %s", msg)


def _condition_id_to_enum(condition_id: str) -> str:
    """Map eBay's numeric condition ids to the enum strings the Inventory API wants.

    See: https://developer.ebay.com/api-docs/sell/inventory/types/slr:ConditionEnum
    """
    return {
        "1000": "NEW",
        "1500": "NEW_OTHER",
        "3000": "USED_EXCELLENT",
        "4000": "USED_VERY_GOOD",
        "5000": "USED_GOOD",
        "6000": "USED_ACCEPTABLE",
    }.get(condition_id, "USED_VERY_GOOD")
