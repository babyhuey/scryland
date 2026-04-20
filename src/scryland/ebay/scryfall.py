"""Scryfall API client — look up MTG card data for eBay listings.

Scryfall is free, stable, and permits image use for listings as long as we
credit Scryfall (https://scryfall.com/docs/api). We're respectful of their
~10 req/s soft limit and add a 100ms per-call delay.

Results are cached on disk (JSON per cache key) with a 7-day TTL so that
re-running `ebay-refresh-titles` or re-listing the same CSV doesn't
re-query cards we just looked up.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

logger = logging.getLogger("scryland")

_BASE = "https://api.scryfall.com"
_PER_CALL_DELAY_S = 0.1  # honour Scryfall's rate-limit guidance

# Cache lives in ~/.cache/scryland/ (follows XDG). 7-day TTL is long
# enough to cover a full list-on-ebay + refresh-titles run, short
# enough that set/image refreshes make it in within a week.
_CACHE_DIR = Path.home() / ".cache" / "scryland" / "scryfall"
_CACHE_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class CardInfo:
    name: str
    set_code: str
    set_name: str
    collector_number: str
    image_url: str | None  # full-size card face
    image_small_url: str | None
    oracle_text: str
    type_line: str
    mana_cost: str
    rarity: str
    scryfall_uri: str
    colors: list[str]  # Scryfall color letters: W/U/B/R/G (empty = colorless)


# Sentinel: cache says "we looked this up and Scryfall didn't have it"
# — distinct from "no cache entry / expired / malformed" which returns None.
_NEGATIVE_CACHE = object()


def _cache_key(name: str, set_name: str | None, collector: str | None) -> str:
    raw = f"{name.lower()}|{(set_name or '').lower()}|{(collector or '').lstrip('0')}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _cache_load(key: str):
    """Return CardInfo if cached, _NEGATIVE_CACHE if we cached a miss,
    None if no (usable) cache entry exists."""
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    stored_at = payload.get("_at", 0)
    if time.time() - stored_at > _CACHE_TTL_SECONDS:
        return None
    if payload.get("_miss"):
        return _NEGATIVE_CACHE
    data = payload.get("card")
    if not data:
        return None
    try:
        return CardInfo(**data)
    except TypeError:
        # Schema drift — ignore cached entry.
        return None


def _cache_save(key: str, info: CardInfo | None) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    payload: dict
    if info is None:
        payload = {"_at": time.time(), "_miss": True}
    else:
        payload = {"_at": time.time(), "card": asdict(info)}
    try:
        (_CACHE_DIR / f"{key}.json").write_text(json.dumps(payload))
    except OSError:
        pass


class ScryfallClient:
    """Async client. Create once per run."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=_BASE,
            timeout=15.0,
            headers={"User-Agent": "Scryland/0.1 (listings helper)"},
        )
        self._owned = client is None

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self) -> ScryfallClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def find_card(
        self,
        name: str,
        set_name: str | None = None,
        collector_number: str | None = None,
    ) -> CardInfo | None:
        """Look up a card by name + optional set + collector number.

        Strategy: try (set_code, collector_number) first (unambiguous), then
        fall back to exact name match scoped by set, then fuzzy name.
        Results are cached on disk for 7 days so repeat runs are fast.
        """
        name = name.split("//")[0].strip()  # front face for double-faced cards

        # Check disk cache first.
        cache_key = _cache_key(name, set_name, collector_number)
        cached = _cache_load(cache_key)
        if cached is _NEGATIVE_CACHE:
            return None
        if cached is not None:
            return cached  # type: ignore[return-value]

        # Try to translate Mythic's set_name → Scryfall set code via search.
        set_code = None
        if set_name:
            set_code = await self._set_name_to_code(set_name)

        info: CardInfo | None = None
        if set_code and collector_number:
            card = await self._get(f"/cards/{set_code}/{collector_number.lstrip('0') or '0'}")
            if card:
                info = self._to_info(card)

        if info is None:
            params: dict[str, str] = {"exact": name}
            if set_code:
                params["set"] = set_code
            card = await self._get("/cards/named", params=params)
            if card:
                info = self._to_info(card)

        if info is None:
            card = await self._get("/cards/named", params={"fuzzy": name})
            if card:
                info = self._to_info(card)

        _cache_save(cache_key, info)
        return info

    async def _set_name_to_code(self, set_name: str) -> str | None:
        """Translate a human set name (e.g., 'Secrets of Strixhaven Mystical
        Archive') to Scryfall's 3-letter set code."""
        # Scryfall's sets list is cheap to fetch and stable. Cache on the
        # client instance.
        if not hasattr(self, "_sets_cache"):
            data = await self._get("/sets")
            sets = (data or {}).get("data", [])
            self._sets_cache = {self._norm(s["name"]): s["code"] for s in sets if "code" in s}
        want = self._norm(set_name)
        if want in self._sets_cache:
            return self._sets_cache[want]
        # Substring match
        for norm_name, code in self._sets_cache.items():
            if want in norm_name or norm_name in want:
                return code
        return None

    @staticmethod
    def _norm(s: str) -> str:
        return " ".join("".join(c.lower() if c.isalnum() else " " for c in s).split())

    @staticmethod
    def _to_info(card: dict) -> CardInfo:
        image_uris = card.get("image_uris") or {}
        # Double-faced cards nest images under card_faces[0]
        if not image_uris and card.get("card_faces"):
            image_uris = card["card_faces"][0].get("image_uris") or {}
        # Prefer png (745×1040) — eBay's recommended 1600px setting isn't
        # achievable but png beats large (672×936) for the zoom feature.
        # Colors: top-level for single-faced; front face for double-faced.
        colors = card.get("colors")
        if colors is None and card.get("card_faces"):
            colors = card["card_faces"][0].get("colors", [])
        if colors is None:
            colors = []
        return CardInfo(
            name=card.get("name", ""),
            set_code=card.get("set", ""),
            set_name=card.get("set_name", ""),
            collector_number=card.get("collector_number", ""),
            image_url=(
                image_uris.get("png") or image_uris.get("large") or image_uris.get("normal")
            ),
            image_small_url=image_uris.get("small"),
            oracle_text=card.get("oracle_text")
            or (
                card.get("card_faces", [{}])[0].get("oracle_text", "")
                if card.get("card_faces")
                else ""
            ),
            type_line=card.get("type_line", ""),
            mana_cost=card.get("mana_cost", ""),
            rarity=card.get("rarity", ""),
            scryfall_uri=card.get("scryfall_uri", ""),
            colors=colors,
        )

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        await asyncio.sleep(_PER_CALL_DELAY_S)
        try:
            r = await self._client.get(path, params=params)
        except Exception:
            logger.warning("Scryfall request failed: %s", path, exc_info=True)
            return None
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            logger.warning("Scryfall %d on %s: %s", r.status_code, path, r.text[:200])
            return None
        return r.json()
