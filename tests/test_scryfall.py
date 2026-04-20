"""Tests for the Scryfall API client."""

from __future__ import annotations

import httpx
import pytest

from scryland.ebay import scryfall as scryfall_mod
from scryland.ebay.scryfall import ScryfallClient


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Redirect the Scryfall disk cache to a temp dir per-test."""
    monkeypatch.setattr(scryfall_mod, "_CACHE_DIR", tmp_path / "scryfall")


def _make_client(transport: httpx.MockTransport) -> ScryfallClient:
    c = httpx.AsyncClient(
        base_url="https://api.scryfall.com",
        transport=transport,
    )
    return ScryfallClient(client=c)


def _card_json(**overrides):
    base = {
        "name": "Reprieve",
        "set": "soa",
        "set_name": "Secrets of Strixhaven: Mystical Archive",
        "collector_number": "9",
        "image_uris": {
            "png": "https://img/png.png",
            "large": "https://img/large.jpg",
            "small": "https://img/small.jpg",
        },
        "oracle_text": "Exile target spell.",
        "type_line": "Instant",
        "mana_cost": "{1}{W}",
        "rarity": "rare",
        "scryfall_uri": "https://scryfall/card",
        "colors": ["W"],
    }
    base.update(overrides)
    return base


class TestFindCard:
    async def test_hit_by_exact_name(self):
        def handler(req):
            if "/sets" in str(req.url):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"name": "Secrets of Strixhaven: Mystical Archive", "code": "soa"},
                        ]
                    },
                )
            if "/cards/soa/9" in str(req.url):
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        sf = _make_client(httpx.MockTransport(handler))
        info = await sf.find_card(
            "Reprieve",
            set_name="Secrets of Strixhaven: Mystical Archive",
            collector_number="9",
        )
        assert info is not None
        assert info.name == "Reprieve"
        assert info.set_code == "soa"
        assert info.rarity == "rare"
        assert info.image_url == "https://img/png.png"  # prefers png
        assert info.colors == ["W"]
        await sf.close()

    async def test_miss_returns_none(self):
        def handler(req):
            return httpx.Response(404)

        sf = _make_client(httpx.MockTransport(handler))
        info = await sf.find_card("Nonexistent")
        assert info is None
        await sf.close()

    async def test_double_faced_card_front_face_image(self):
        def handler(req):
            if "/sets" in str(req.url):
                return httpx.Response(200, json={"data": []})
            if "/cards/named" in str(req.url):
                return httpx.Response(
                    200,
                    json={
                        "name": "Honorbound Page // Forum's Favor",
                        "set": "sos",
                        "set_name": "Secrets of Strixhaven",
                        "collector_number": "19",
                        "card_faces": [
                            {
                                "image_uris": {"png": "https://front.png"},
                                "oracle_text": "Front text",
                            },
                            {"image_uris": {"png": "https://back.png"}, "oracle_text": "Back text"},
                        ],
                        "type_line": "Creature // Instant",
                        "rarity": "common",
                        "colors": ["W"],
                    },
                )
            return httpx.Response(404)

        sf = _make_client(httpx.MockTransport(handler))
        info = await sf.find_card("Honorbound Page // Forum's Favor")
        assert info is not None
        assert info.image_url == "https://front.png"  # front face
        assert info.oracle_text == "Front text"
        await sf.close()

    async def test_cache_hit_avoids_network(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if "/sets" in str(req.url):
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json=_card_json())

        sf = _make_client(httpx.MockTransport(handler))
        # First lookup hits the network.
        info1 = await sf.find_card("Reprieve")
        assert info1 is not None
        first_count = calls["n"]
        # Second lookup identical — should be served from disk.
        info2 = await sf.find_card("Reprieve")
        assert info2 is not None
        assert info2.name == info1.name
        assert calls["n"] == first_count  # no extra network calls
        await sf.close()

    async def test_cache_stores_miss(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return httpx.Response(404)

        sf = _make_client(httpx.MockTransport(handler))
        assert await sf.find_card("Nonexistent") is None
        after_first = calls["n"]
        # Second call should return None from cache, no new network hits.
        assert await sf.find_card("Nonexistent") is None
        assert calls["n"] == after_first
        await sf.close()

    async def test_image_fallback_to_large_then_normal(self):
        def handler(req):
            if "/sets" in str(req.url):
                return httpx.Response(200, json={"data": []})
            if "/cards/named" in str(req.url):
                # No png, has large
                return httpx.Response(
                    200,
                    json=_card_json(
                        image_uris={
                            "large": "https://img/large.jpg",
                        }
                    ),
                )
            return httpx.Response(404)

        sf = _make_client(httpx.MockTransport(handler))
        info = await sf.find_card("Reprieve")
        assert info.image_url == "https://img/large.jpg"
        await sf.close()
