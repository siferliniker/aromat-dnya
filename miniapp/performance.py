"""Small, on-demand Fragella integration; no personal fields or bulk data export."""

import asyncio
from collections import OrderedDict
import json
import hashlib
import re
import time
import unicodedata

import aiohttp
from sqlalchemy import Column, Float, JSON, MetaData, String, Table, delete, select
from sqlalchemy.dialects.sqlite import insert


CACHE = Table("performance_cache", MetaData(), Column("key", String(64), primary_key=True),
              Column("payload", JSON), Column("expires", Float, nullable=False))


LONGEVITY = {
    "poor": "Очень низкая", "weak": "Низкая", "moderate": "Умеренная",
    "long lasting": "Высокая", "very long lasting": "Очень высокая",
}
SILLAGE = {"intimate": "Близко к коже", "moderate": "Умеренный",
           "soft": "Мягкий", "strong": "Сильный", "enormous": "Очень сильный"}


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    return re.sub(r"[^a-z0-9]", "", value)


def exact_match(card, row):
    brand = normalize(row.get("Brand", ""))
    target_brand = normalize(card.get("house", ""))
    if not target_brand or brand not in (target_brand, target_brand + "perfumes"):
        return False
    name = normalize(row.get("Name", ""))
    expected = normalize(card.get("name", ""))
    if name not in (expected, target_brand + expected, brand + expected):
        return False
    concentration = normalize(card.get("concentration", ""))
    actual = normalize(row.get("OilType", ""))
    return not concentration or actual == concentration


class FragellaPerformance:
    """Free-plan only, at most 20 searches per UTC day; bounded encrypted cache."""

    def __init__(self, api_key, engine=None):
        self.key = api_key
        self.engine = engine
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.checked = False
        self.disabled = False
        self.day = ""
        self.searches = 0
        self.status = "not_configured" if not api_key else "ready"
        if engine is not None:
            CACHE.metadata.create_all(engine)
            with engine.connect() as connection:
                for row in connection.execute(select(CACHE).where(
                        CACHE.c.expires > time.time()).order_by(CACHE.c.expires.desc()).limit(64)).mappings():
                    self.cache[row["key"]] = (row["expires"], row["payload"])

    @staticmethod
    def cache_key(card):
        return hashlib.sha256(json.dumps([card.get(k) for k in
            ("house", "name", "concentration")], ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def record(row):
        longevity = LONGEVITY.get(str(row.get("Longevity", "")).casefold())
        sillage = SILLAGE.get(str(row.get("Sillage", "")).casefold())
        if not longevity and not sillage:
            return None
        return {"longevity": longevity, "sillage": sillage,
            "source_name": "Fragella", "source_url": "https://api.fragella.com/",
            "basis": "provider_aggregate", "provider_id": str(row.get("_id", "")),
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "confidence": str(row.get("Confidence", "")).casefold(), "sample_size": None}

    def remember(self, card, result):
        key = self.cache_key(card)
        expires = time.time() + (86400 if result else 900)
        self.cache[key] = (expires, result)
        self.cache.move_to_end(key)
        while len(self.cache) > 64:
            self.cache.popitem(last=False)
        if self.engine is not None:
            with self.engine.begin() as connection:
                statement = insert(CACHE).values(key=key, expires=expires, payload=result)
                connection.execute(statement.on_conflict_do_update(index_elements=[CACHE.c.key],
                    set_={"expires": expires, "payload": result}))
                keep = select(CACHE.c.key).order_by(CACHE.c.expires.desc()).limit(64)
                connection.execute(delete(CACHE).where(CACHE.c.key.not_in(keep)))

    def cached(self, card):
        if not card or card.get("performance"):
            return card
        cached = self.cache.get(self.cache_key(card))
        return {**card, "performance": cached[1]} if cached and cached[0] > time.time() and cached[1] else card

    async def _get(self, session, path, params=None):
        async with session.get("https://api.fragella.com/api/v1/" + path,
                params=params, headers={"x-api-key": self.key}, allow_redirects=False) as response:
            if response.status != 200:
                self.status = "unavailable_" + str(response.status)
                if response.status in (401, 402, 403, 429):
                    self.disabled = True
                return None
            raw = await response.content.read(1_000_001)
            if len(raw) > 1_000_000:
                self.status = "response_too_large"
                return None
            return json.loads(raw)

    async def enrich(self, card):
        card = self.cached(card)
        if not card or card.get("performance") or not self.key or self.disabled:
            return card
        async with self.lock:
            if self.disabled:
                return self.cached(card)
            key = self.cache_key(card)
            cached = self.cache.get(key)
            if cached and cached[0] > time.time():
                return {**card, "performance": cached[1]} if cached[1] else card
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if self.day != today:
                self.day, self.searches = today, 0
            if self.searches >= 20:
                self.status = "daily_limit"
                return card
            result = None
            try:
                timeout = aiohttp.ClientTimeout(total=3, connect=2)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    usage = await self._get(session, "usage")
                    if not isinstance(usage, dict):
                        return card
                    # Verify before every uncached lookup: changing plans must not enable overages.
                    if str(usage.get("plan", "")).casefold() != "free":
                        self.disabled, self.status = True, "free_plan_required"
                        return card
                    if usage.get("usage", {}).get("requests_remaining", 0) <= 0:
                        self.disabled, self.status = True, "monthly_quota_exhausted"
                        return card
                    self.searches += 1
                    payload = await self._get(session, "fragrances", {
                        "search": card["name"], "brand": card["house"], "limit": 10})
                rows = payload.get("data", []) if isinstance(payload, dict) else payload
                matches = [r for r in rows if isinstance(r, dict) and exact_match(card, r)] if isinstance(rows, list) else []
                if len(matches) == 1:
                    row = matches[0]
                    result = self.record(row)
                self.status = "available" if result else "no_exact_match"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
                self.status = "temporarily_unavailable"
            self.remember(card, result)
            return {**card, "performance": result} if result else card
