"""Tests for EbayClient — uses httpx.MockTransport for network calls."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from scryland.config import ScrylandConfig
from scryland.ebay.client import (
    EbayClient,
    _condition_id_to_enum,
    _extract_warnings,
)


@pytest.fixture
def config():
    c = ScrylandConfig()
    c = c.model_copy(
        update={
            "ebay_fulfillment_policy_id": "FID",
            "ebay_payment_policy_id": "PID",
            "ebay_return_policy_id": "RID",
            "ebay_merchant_location_key": "default",
            "ebay_shipping_cost": 0.99,
            "ebay_seller_username": "myuser",
        }
    )
    return c


def _make_client(config, transport: httpx.MockTransport) -> EbayClient:
    """Build an EbayClient whose _http goes through our mock transport."""
    auth = MagicMock()
    auth.api_base = "https://api.ebay.com"
    auth.access_token = AsyncMock(return_value="user-tok")
    auth.app_access_token = AsyncMock(return_value="app-tok")
    client = EbayClient(config, auth, passphrase="pw")
    # Replace the httpx client with one using our mock transport.
    client._http = httpx.AsyncClient(
        base_url="https://api.ebay.com",
        transport=transport,
        timeout=5.0,
    )
    return client


class TestConditionEnumMapping:
    def test_known_values(self):
        assert _condition_id_to_enum("1000") == "NEW"
        assert _condition_id_to_enum("4000") == "USED_VERY_GOOD"
        assert _condition_id_to_enum("6000") == "USED_ACCEPTABLE"

    def test_unknown_falls_back(self):
        assert _condition_id_to_enum("9999") == "USED_VERY_GOOD"


class TestExtractWarnings:
    def test_no_body(self):
        r = httpx.Response(200, text="")
        warnings: list[str] = []
        _extract_warnings(r, warnings)
        assert warnings == []

    def test_benign_warnings_suppressed(self):
        r = httpx.Response(
            200,
            json={
                "warnings": [
                    {"message": "Funds from your sales may be unavailable"},
                ],
            },
        )
        warnings: list[str] = []
        _extract_warnings(r, warnings)
        assert warnings == []  # benign ones suppressed

    def test_unknown_warning_surfaces(self):
        r = httpx.Response(
            200,
            json={
                "warnings": [{"message": "Weird unknown thing"}],
            },
        )
        warnings: list[str] = []
        _extract_warnings(r, warnings)
        assert warnings == ["Weird unknown thing"]


class TestWithdrawOffer:
    async def test_204_returns_true(self, config):
        def handler(req):
            return httpx.Response(204)

        client = _make_client(config, httpx.MockTransport(handler))
        assert await client.withdraw_offer("OFFER1") is True
        await client._http.aclose()

    async def test_already_ended_errorid_returns_true(self, config):
        def handler(req):
            return httpx.Response(
                400,
                json={
                    "errors": [{"errorId": 25001, "message": "Offer is not published."}],
                },
            )

        client = _make_client(config, httpx.MockTransport(handler))
        assert await client.withdraw_offer("OFFER1") is True
        await client._http.aclose()

    async def test_already_withdrawn_message_returns_true(self, config):
        def handler(req):
            return httpx.Response(
                400,
                json={
                    "errors": [{"errorId": 99999, "message": "listing is not active"}],
                },
            )

        client = _make_client(config, httpx.MockTransport(handler))
        assert await client.withdraw_offer("OFFER1") is True
        await client._http.aclose()

    async def test_genuine_failure_returns_false(self, config):
        def handler(req):
            return httpx.Response(
                500,
                json={
                    "errors": [{"errorId": 25002, "message": "Server error"}],
                },
            )

        client = _make_client(config, httpx.MockTransport(handler))
        assert await client.withdraw_offer("OFFER1") is False
        await client._http.aclose()


class TestUpdateOfferPrice:
    async def test_round_trip_verifies_new_price(self, config):
        calls = []

        def handler(req):
            calls.append((req.method, str(req.url)))
            if req.method == "GET":
                # Current offer or verify GET — return the updated price.
                return httpx.Response(
                    200,
                    json={
                        "pricingSummary": {"price": {"value": "2.49", "currency": "USD"}},
                        "availableQuantity": 1,
                    },
                )
            if req.method == "PUT":
                return httpx.Response(200, json={})
            return httpx.Response(500)

        client = _make_client(config, httpx.MockTransport(handler))
        ok = await client.update_offer_price("OFF1", 2.49, 1)
        assert ok is True
        # GET(current) + PUT + GET(verify) = 3 calls
        assert len(calls) == 3
        await client._http.aclose()

    async def test_verify_sees_wrong_price_returns_false(self, config):
        def handler(req):
            if req.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "pricingSummary": {"price": {"value": "1.00"}},
                        "availableQuantity": 1,
                    },
                )
            if req.method == "PUT":
                return httpx.Response(200, json={})
            return httpx.Response(500)

        client = _make_client(config, httpx.MockTransport(handler))
        # Requested $2.49 but verify GET shows $1.00 → should return False
        ok = await client.update_offer_price("OFF1", 2.49, 1)
        assert ok is False
        await client._http.aclose()

    async def test_retries_on_5xx(self, config):
        state = {"calls": 0}

        def handler(req):
            state["calls"] += 1
            if req.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "pricingSummary": {"price": {"value": "2.49"}},
                        "availableQuantity": 1,
                    },
                )
            # First PUT 500, second PUT OK
            if state["calls"] <= 2:  # GET + first PUT
                return httpx.Response(
                    500,
                    json={
                        "errors": [{"errorId": 25002, "message": "try again"}],
                    },
                )
            return httpx.Response(200, json={})

        client = _make_client(config, httpx.MockTransport(handler))
        # Patch asyncio.sleep to zero so retry backoff is instant.
        import asyncio

        orig_sleep = asyncio.sleep
        asyncio.sleep = AsyncMock()  # type: ignore
        try:
            ok = await client.update_offer_price("OFF1", 2.49, 1)
        finally:
            asyncio.sleep = orig_sleep  # type: ignore
        assert ok is True
        await client._http.aclose()


class TestFindLowestPrice:
    async def test_filters_foil_mismatch(self, config):
        """Non-foil search: results with 'foil' in title should be dropped."""

        def handler(req):
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "title": "Reprieve Secrets of Strixhaven #9 FOIL",
                            "price": {"value": "0.50"},
                            "seller": {"username": "x"},
                        },
                        {
                            "title": "Reprieve Secrets of Strixhaven #9 NM",
                            "price": {"value": "1.50"},
                            "shippingOptions": [{"shippingCost": {"value": "0.73"}}],
                            "seller": {"username": "x"},
                        },
                    ],
                },
            )

        client = _make_client(config, httpx.MockTransport(handler))
        lowest = await client.find_lowest_price(
            "Reprieve",
            "Secrets of Strixhaven",
            "9",
            is_foil=False,
            include_foil=False,
        )
        # Only the non-foil $1.50+$0.73=$2.23 matches.
        assert lowest == pytest.approx(2.23)
        await client._http.aclose()

    async def test_filters_own_seller(self, config):
        def handler(req):
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "title": "Reprieve Secrets of Strixhaven NM",
                            "price": {"value": "1.00"},
                            "seller": {"username": "myuser"},
                        },  # ours
                        {
                            "title": "Reprieve Secrets of Strixhaven NM",
                            "price": {"value": "2.00"},
                            "seller": {"username": "competitor"},
                        },
                    ],
                },
            )

        client = _make_client(config, httpx.MockTransport(handler))
        lowest = await client.find_lowest_price(
            "Reprieve",
            "Secrets of Strixhaven",
            "9",
            is_foil=False,
        )
        # Our $1.00 is excluded; competitor $2.00 wins.
        assert lowest == 2.0
        await client._http.aclose()

    async def test_filters_bad_terms(self, config):
        def handler(req):
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "title": "Reprieve Secrets of Strixhaven 4x PLAYSET",
                            "price": {"value": "1.00"},
                            "seller": {"username": "x"},
                        },
                        {
                            "title": "Reprieve Secrets of Strixhaven NM",
                            "price": {"value": "3.00"},
                            "seller": {"username": "x"},
                        },
                    ],
                },
            )

        client = _make_client(config, httpx.MockTransport(handler))
        lowest = await client.find_lowest_price(
            "Reprieve",
            "Secrets of Strixhaven",
            "9",
            is_foil=False,
        )
        assert lowest == 3.0
        await client._http.aclose()

    async def test_cheapest_shipping_option_picked(self, config):
        def handler(req):
            return httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "title": "Reprieve Secrets of Strixhaven NM",
                            "price": {"value": "1.00"},
                            "shippingOptions": [
                                {"shippingCost": {"value": "5.00"}},  # expedited first
                                {"shippingCost": {"value": "0.71"}},  # cheap second
                            ],
                            "seller": {"username": "x"},
                        }
                    ],
                },
            )

        client = _make_client(config, httpx.MockTransport(handler))
        lowest = await client.find_lowest_price(
            "Reprieve",
            "Secrets of Strixhaven",
            "9",
            is_foil=False,
        )
        # $1.00 + cheapest $0.71 = $1.71 (not $6)
        assert lowest == pytest.approx(1.71)
        await client._http.aclose()

    async def test_no_matches_returns_none(self, config):
        def handler(req):
            return httpx.Response(200, json={"itemSummaries": []})

        client = _make_client(config, httpx.MockTransport(handler))
        lowest = await client.find_lowest_price(
            "Reprieve",
            "X",
            "9",
            is_foil=False,
        )
        assert lowest is None
        await client._http.aclose()

    async def test_http_error_raises(self, config):
        def handler(req):
            return httpx.Response(500, text="boom")

        client = _make_client(config, httpx.MockTransport(handler))
        with pytest.raises(RuntimeError, match="Browse search"):
            await client.find_lowest_price(
                "Reprieve",
                "X",
                "9",
                is_foil=False,
            )
        await client._http.aclose()


class TestCreateMerchantLocation:
    async def test_success(self, config):
        def handler(req):
            return httpx.Response(204)

        client = _make_client(config, httpx.MockTransport(handler))
        await client.create_merchant_location(
            "default",
            country="US",
            city="Durham",
            state="NC",
            postal_code="27712",
        )
        await client._http.aclose()

    async def test_already_exists_treated_as_success(self, config):
        def handler(req):
            return httpx.Response(409, json={"errors": [{"message": "exists"}]})

        client = _make_client(config, httpx.MockTransport(handler))
        # Should NOT raise
        await client.create_merchant_location(
            "default",
            country="US",
            city="Durham",
            state="NC",
            postal_code="27712",
        )
        await client._http.aclose()

    async def test_other_errors_raise(self, config):
        def handler(req):
            return httpx.Response(500, text="boom")

        client = _make_client(config, httpx.MockTransport(handler))
        with pytest.raises(RuntimeError, match="create location failed"):
            await client.create_merchant_location(
                "default",
                country="US",
                city="X",
                state="Y",
                postal_code="Z",
            )
        await client._http.aclose()


class TestListBusinessPolicies:
    async def test_returns_ids_and_names(self, config):
        def handler(req):
            url = str(req.url)
            if "fulfillment_policy" in url:
                return httpx.Response(
                    200,
                    json={
                        "fulfillmentPolicies": [
                            {"fulfillmentPolicyId": "F1", "name": "Ship A"},
                        ],
                    },
                )
            if "payment_policy" in url:
                return httpx.Response(
                    200,
                    json={
                        "paymentPolicies": [
                            {"paymentPolicyId": "P1", "name": "Pay A"},
                        ],
                    },
                )
            if "return_policy" in url:
                return httpx.Response(
                    200,
                    json={
                        "returnPolicies": [
                            {"returnPolicyId": "R1", "name": "Ret A"},
                        ],
                    },
                )
            return httpx.Response(404)

        client = _make_client(config, httpx.MockTransport(handler))
        policies = await client.list_business_policies()
        assert policies["fulfillment"][0]["id"] == "F1"
        assert policies["payment"][0]["id"] == "P1"
        assert policies["return"][0]["id"] == "R1"
        await client._http.aclose()

    async def test_empty_on_error(self, config):
        def handler(req):
            return httpx.Response(500)

        client = _make_client(config, httpx.MockTransport(handler))
        policies = await client.list_business_policies()
        assert all(policies[k] == [] for k in ("fulfillment", "payment", "return"))
        await client._http.aclose()


class TestOptInToBusinessPolicies:
    async def test_204_is_success(self, config):
        def handler(req):
            return httpx.Response(204)

        client = _make_client(config, httpx.MockTransport(handler))
        await client.opt_in_to_business_policies()  # no raise
        await client._http.aclose()

    async def test_409_treated_as_already_opted(self, config):
        def handler(req):
            return httpx.Response(409)

        client = _make_client(config, httpx.MockTransport(handler))
        await client.opt_in_to_business_policies()  # no raise
        await client._http.aclose()


class TestUpdateFulfillmentShipping:
    async def test_changes_cost(self, config):
        state = {"put_body": None}

        def handler(req):
            if req.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "name": "P",
                        "marketplaceId": "EBAY_US",
                        "shippingOptions": [
                            {
                                "optionType": "DOMESTIC",
                                "shippingServices": [
                                    {
                                        "shippingCost": {"value": "4.99", "currency": "USD"},
                                    }
                                ],
                            }
                        ],
                    },
                )
            if req.method == "PUT":
                state["put_body"] = json.loads(req.content)
                return httpx.Response(200, json={})
            return httpx.Response(500)

        client = _make_client(config, httpx.MockTransport(handler))
        assert await client.update_fulfillment_shipping_cost("FID", "0.99")
        body = state["put_body"]
        svc = body["shippingOptions"][0]["shippingServices"][0]
        assert svc["shippingCost"]["value"] == "0.99"
        await client._http.aclose()

    async def test_same_as_system_treated_success(self, config):
        def handler(req):
            if req.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "shippingOptions": [],
                    },
                )
            return httpx.Response(
                400,
                json={
                    "errors": [
                        {
                            "message": "Business Profile information in the request is the same as in the system"
                        }
                    ]
                },
            )

        client = _make_client(config, httpx.MockTransport(handler))
        # PUT 400 with "same as in the system" → treat as success.
        assert await client.update_fulfillment_shipping_cost("FID", "0.99")
        await client._http.aclose()


class TestPublishListing:
    async def test_happy_path_creates_offer_and_publishes(self, config):
        """Walks inventory PUT → offer lookup (404) → offer POST → publish."""
        from dataclasses import dataclass

        from scryland.ebay.listing import build_listing

        @dataclass
        class C:
            card_name: str = "Reprieve"
            set_name: str = "Secrets of Strixhaven: Mystical Archive"
            collector_number: str = "9"
            tcg_condition: str = "Near Mint"
            quantity: int = 1
            is_foil: bool = False
            effective_price = 2.49

        def handler(req):
            url, method = str(req.url), req.method
            if "inventory_item/" in url and method == "PUT":
                return httpx.Response(204)
            if "/offer" in url and method == "GET":
                # no existing offer
                return httpx.Response(200, json={"offers": []})
            if url.endswith("/offer") and method == "POST":
                return httpx.Response(201, json={"offerId": "OFF1"})
            if "/publish" in url and method == "POST":
                return httpx.Response(200, json={"listingId": "LST1"})
            return httpx.Response(500, text=f"unmocked {method} {url}")

        client = _make_client(config, httpx.MockTransport(handler))
        listing = build_listing(C(), None, 2.49)
        result = await client.publish_listing(listing, draft=False)
        assert result.offer_id == "OFF1"
        assert result.listing_id == "LST1"
        assert result.draft is False
        await client._http.aclose()

    async def test_draft_mode_skips_publish(self, config):
        from dataclasses import dataclass

        from scryland.ebay.listing import build_listing

        @dataclass
        class C:
            card_name: str = "X"
            set_name: str = "Y"
            collector_number: str = "1"
            tcg_condition: str = "Near Mint"
            quantity: int = 1
            is_foil: bool = False
            effective_price = 1.00

        calls = []

        def handler(req):
            calls.append((req.method, str(req.url)))
            if "/publish" in str(req.url):
                pytest.fail("draft mode should not call publish")
            if req.method == "PUT":
                return httpx.Response(204)
            if req.method == "GET":
                return httpx.Response(200, json={"offers": []})
            if req.method == "POST":
                return httpx.Response(201, json={"offerId": "OFF2"})
            return httpx.Response(500)

        client = _make_client(config, httpx.MockTransport(handler))
        listing = build_listing(C(), None, 1.00)
        result = await client.publish_listing(listing, draft=True)
        assert result.draft is True
        assert result.listing_id is None
        await client._http.aclose()


class TestGetOwnSellerUsername:
    async def test_uses_configured_value_first(self, config):
        """If SCRYLAND_EBAY_SELLER_USERNAME is set, no API call needed."""

        def handler(req):
            pytest.fail("Should not hit eBay when username is configured")

        client = _make_client(config, httpx.MockTransport(handler))
        name = await client.get_own_seller_username()
        assert name == "myuser"
        await client._http.aclose()

    async def test_browse_fallback_from_listing(self, config):
        config = config.model_copy(update={"ebay_seller_username": ""})

        def handler(req):
            url = str(req.url)
            if "/identity/" in url:
                return httpx.Response(404)
            if "/item/" in url:
                return httpx.Response(
                    200,
                    json={
                        "seller": {"username": "found_via_browse"},
                    },
                )
            return httpx.Response(500)

        client = _make_client(config, httpx.MockTransport(handler))
        name = await client.get_own_seller_username(sample_listing_id="1234")
        assert name == "found_via_browse"
        await client._http.aclose()

    async def test_all_failed_returns_none_and_caches(self, config):
        config = config.model_copy(update={"ebay_seller_username": ""})
        call_count = {"identity": 0}

        def handler(req):
            url = str(req.url)
            if "/identity/" in url:
                call_count["identity"] += 1
                return httpx.Response(404)
            return httpx.Response(500)

        client = _make_client(config, httpx.MockTransport(handler))
        assert await client.get_own_seller_username() is None
        # Second call must not retry (cache sentinel)
        assert await client.get_own_seller_username() is None
        assert call_count["identity"] == 1
        await client._http.aclose()
