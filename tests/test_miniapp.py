"""Focused Mini App acceptance checks; SQLite, catalogue and Telegram keys are local fixtures."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from pathlib import Path
import time
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
import pytest
from sqlalchemy import text

from storage.models import Base


ROOT = Path(__file__).resolve().parents[1]
OWNER = 777000123
TOKEN = "123456:miniapp-synthetic-test-secret"


def signed_data(user_id=OWNER, *, auth_date=None, extra=(), user=None):
    pairs = [
        ("auth_date", str(int(time.time()) if auth_date is None else auth_date)),
        ("query_id", "miniapp-qa-query"),
        ("user", json.dumps(user if user is not None else {"id": user_id, "first_name": "QA"},
                            separators=(",", ":"), ensure_ascii=False)),
        *extra,
    ]
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode([*pairs, ("hash", digest)])


def headers(user_id=OWNER):
    return {"Authorization": f"tma {signed_data(user_id)}"}


def rows(engine, query):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(text(query)).mappings()]


@pytest.fixture
def catalogue(qa_seed, qa_engine, tmp_path):
    ids = qa_seed(4)
    raw = [json.loads(line) for line in (ROOT / "data/scento/perfumes.jsonl").read_text(
        encoding="utf-8").splitlines()[:4]]
    with qa_engine.begin() as connection:
        for perfume_id, item in zip(ids, raw):
            connection.execute(text("""
                UPDATE perfumes SET name=:name,house=:house,external_id=:external,
                    release_year=:year,source_url=:url WHERE id=:id
            """), {"id": perfume_id, "external": item["id"], "name": item["name"],
                   "house": item["house"], "year": item["year"], "url": item["url"]})
    path = tmp_path / "catalogue.jsonl"
    # Reverse order proves metadata is joined by the publisher ID, never row position.
    path.write_text("\n".join(json.dumps(item) for item in reversed(raw)), encoding="utf-8")
    return ids, dict(zip(ids, raw)), path


@pytest.fixture
def app_factory(qa_engine, qa_config, catalogue):
    from miniapp.app import create_app

    def make(config=qa_config):
        return create_app(engine=qa_engine, bot_token=TOKEN, owner_id=OWNER,
                          config=config, catalog_path=catalogue[2])
    return make


@asynccontextmanager
async def client_for(factory):
    client = TestClient(TestServer(factory()))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def state(client):
    response = await client.get("/api/state", headers=headers())
    assert response.status == 200, await response.text()
    return await response.json()


async def react(client, current, reaction, **changes):
    body = {"perfume_id": current["current"]["id"], "revision": current["revision"],
            "reaction": reaction, **changes}
    response = await client.post("/api/react", headers=headers(), json=body)
    assert response.status == 200, await response.text()
    return await response.json()


def test_init_data_requires_official_signature_freshness_unique_fields_and_owner():
    from miniapp.auth import AuthError, validate_init_data

    now = int(time.time())
    good = signed_data(auth_date=now, user={"id": OWNER, "first_name": "Тест + & ="})
    assert validate_init_data(good, TOKEN, OWNER, now=now) == OWNER
    invalid = [
        "", "user=%ZZ", good.replace("hash=", "hash=0", 1),
        good.replace("miniapp-qa-query", "changed"),
        signed_data(auth_date=now - 172800), signed_data(auth_date=now + 3600),
        signed_data(auth_date=now, extra=(("auth_date", str(now)),)),
        signed_data(auth_date=now, extra=(("query_id", "second"),)),
        good + "&hash=" + "0" * 64,
        signed_data(auth_date=now, user={"id": str(OWNER)}),
        signed_data(auth_date=now, user={"id": True}),
        signed_data(OWNER + 1, auth_date=now),
    ]
    for raw in invalid:
        with pytest.raises(AuthError):
            validate_init_data(raw, TOKEN, OWNER, now=now)


def test_all_api_routes_reject_missing_bad_and_nonowner_auth_without_leaking_secrets(app_factory, qa_engine):
    async def scenario():
        async with client_for(app_factory) as client:
            for method, path in (("get", "/api/state"), ("get", "/api/library?kind=wishlist"),
                                 ("post", "/api/react")):
                for supplied, expected in (({}, 401), ({"Authorization": "tma invalid"}, 401),
                                           (headers(OWNER + 1), 403)):
                    kwargs = {"headers": supplied}
                    if method == "post":
                        kwargs["json"] = {"perfume_id": 1, "revision": 1, "reaction": "want"}
                    response = await getattr(client, method)(path, **kwargs)
                    assert response.status == expected
                    body = await response.text()
                    assert TOKEN not in body
                    assert supplied.get("Authorization", "not-present") not in body
            assert rows(qa_engine, "SELECT * FROM user_feedback") == []
    asyncio.run(scenario())


def test_state_uses_matching_real_catalogue_metadata_and_preserves_existing_schema(app_factory, qa_engine, catalogue):
    with qa_engine.connect() as connection:
        before = {name: connection.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": name}).scalar_one() for name in Base.metadata.tables}

    async def scenario():
        async with client_for(app_factory) as client:
            first = await state(client)
            assert first == await state(client)
            card = first["current"]
            expected = catalogue[1][card["id"]]
            assert (card["name"], card["house"]) == (expected["name"], expected["house"])
            assert card["family"] == expected["family"]
            assert card["description"] == expected["description"]
            for level in ("top", "heart", "base"):
                assert card["notes"][level] == expected[f"{level}_notes"]
            assert card.get("reviews", []) == []
            assert first["counts"] == {"wishlist": 0, "known": 0}
            assert first["seen_count"] == 0
            assert first["remaining"] == 4
            assert type(first["revision"]) is int
            assert TOKEN not in json.dumps(first)
    asyncio.run(scenario())
    with qa_engine.connect() as connection:
        assert "miniapp_state" not in Base.metadata.tables
        assert connection.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='miniapp_state'"
        )).scalar_one() == 1
        for name, ddl in before.items():
            assert connection.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
                {"name": name}).scalar_one() == ddl


def test_reactions_advance_once_and_persist_in_real_feedback_and_both_libraries(app_factory, qa_engine):
    recorded = {}

    async def scenario():
        for index, reaction in enumerate(("want", "known", "bought", "not_for_me"), 1):
            # A fresh application creates a fresh repository against the same database.
            async with client_for(app_factory) as client:
                current = await state(client)
                perfume_id = current["current"]["id"]
                assert perfume_id not in recorded
                recorded[perfume_id] = reaction
                result = await react(client, current, reaction)
                assert result["applied"] is True
                assert result["revision"] > current["revision"]
                assert result["seen_count"] == index
                assert result["remaining"] == 4 - index
                if index == 4:
                    assert result["current"] is None
                else:
                    assert result["current"]["id"] != perfume_id
                assert {row["perfume_id"]: row["reaction"] for row in rows(
                    qa_engine, f"SELECT * FROM user_feedback WHERE user_id={OWNER}")} == recorded
        async with client_for(app_factory) as client:
            final = await state(client)
            assert final["current"] is None
            assert final["counts"] == {"wishlist": 1, "known": 2}
            for kind, reactions in (("wishlist", {"want"}), ("known", {"known", "bought"})):
                response = await client.get(f"/api/library?kind={kind}", headers=headers())
                assert response.status == 200
                library = await response.json()
                expected = {key: value for key, value in recorded.items() if value in reactions}
                assert library["count"] == len(expected)
                assert {item["id"]: item["reaction"] for item in library["items"]} == expected
    asyncio.run(scenario())


def test_concurrent_reactions_replays_and_stale_cards_cannot_overwrite_feedback(app_factory, qa_engine, catalogue):
    async def scenario():
        async with client_for(app_factory) as client:
            first = await state(client)
            results = await asyncio.gather(react(client, first, "want"), react(client, first, "known"))
            assert sorted(result["applied"] for result in results) == [False, True]
            winner = next(result for result in results if result["applied"])
            persisted = rows(qa_engine, "SELECT * FROM user_feedback")
            assert len(persisted) == 1
            second = await state(client)
            assert second["current"]["id"] != first["current"]["id"]
            for reaction in ("want", "known", "bought", "not_for_me"):
                replay = await react(client, first, reaction)
                assert replay["applied"] is False
                assert replay["revision"] == winner["revision"]
                assert replay["current"] == second["current"]
            stale = await react(client, second, "not_for_me", revision=first["revision"])
            assert stale["applied"] is False
            unseen = next(value for value in catalogue[0] if value not in (
                first["current"]["id"], second["current"]["id"]))
            forged = await react(client, second, "bought", perfume_id=unseen)
            assert forged["applied"] is False
            assert await state(client) == second
            assert rows(qa_engine, "SELECT * FROM user_feedback") == persisted
    asyncio.run(scenario())


def test_invalid_reaction_payloads_are_400_and_leave_current_and_feedback_unchanged(app_factory, qa_engine):
    async def scenario():
        async with client_for(app_factory) as client:
            current = await state(client)
            body = {"perfume_id": current["current"]["id"], "revision": current["revision"], "reaction": "want"}
            invalid = [[], {}, {**body, "reaction": "invented"}, {**body, "reaction": None},
                       {**body, "perfume_id": True}, {**body, "perfume_id": str(body["perfume_id"])},
                       {**body, "revision": True}, {**body, "revision": str(body["revision"])},
                       {**body, "user_id": OWNER + 1}]
            for payload in invalid:
                response = await client.post("/api/react", headers=headers(), json=payload)
                assert response.status == 400, await response.text()
                assert await state(client) == current
            response = await client.post("/api/react", headers={**headers(), "Content-Type": "application/json"}, data="{")
            assert response.status == 400
            response = await client.get("/api/library?kind=invented", headers=headers())
            assert response.status == 400
            assert await state(client) == current
            assert rows(qa_engine, "SELECT * FROM user_feedback") == []
    asyncio.run(scenario())


def test_existing_bot_feedback_and_sent_history_produce_honest_exhausted_state(app_factory, qa_engine, catalogue):
    ids = catalogue[0]
    with qa_engine.begin() as connection:
        for perfume_id, reaction in zip(ids, ("want", "known", "bought")):
            connection.execute(text("""
                INSERT INTO user_feedback(user_id,perfume_id,reaction) VALUES (:owner,:id,:reaction)
            """), {"owner": OWNER, "id": perfume_id, "reaction": reaction})
        connection.execute(text("""
            INSERT INTO sent_log(user_id,perfume_id,local_date,status,sent_at,telegram_message_id)
            VALUES (:owner,:id,'2026-11-01','sent','2026-11-01 04:00:00',123)
        """), {"owner": OWNER, "id": ids[3]})

    async def scenario():
        async with client_for(app_factory) as client:
            result = await state(client)
            assert result["current"] is None
            assert result["counts"] == {"wishlist": 1, "known": 2}
            assert result["seen_count"] == 4
            assert result["remaining"] == 0
            response = await client.post("/api/react", headers=headers(), json={
                "perfume_id": ids[0], "revision": result["revision"], "reaction": "not_for_me"})
            assert response.status == 200
            assert (await response.json())["applied"] is False
            assert await state(client) == result
    asyncio.run(scenario())
    assert len(rows(qa_engine, "SELECT * FROM user_feedback")) == 3
    assert len(rows(qa_engine, "SELECT * FROM sent_log")) == 1


def test_disabled_or_unapproved_sources_supply_no_cards_without_external_calls(app_factory, qa_engine, qa_config, monkeypatch):
    # Any adapter/LLM access would be a bug for this offline Mini App.
    from collectors.dataset import DatasetAdapter
    from collectors.telegram import TelegramAdapter
    from collectors.youtube import YouTubeAdapter
    from llm.client import OpenAIResponsesClient

    async def forbidden(*args, **kwargs):
        raise AssertionError("Mini App attempted an external collection operation")
    for adapter in (DatasetAdapter, YouTubeAdapter):
        monkeypatch.setattr(adapter, "collect", forbidden)
    monkeypatch.setattr(OpenAIResponsesClient, "parse", forbidden)
    def forbidden_update(*args, **kwargs):
        raise AssertionError("Mini App attempted Telegram collection")
    monkeypatch.setattr(TelegramAdapter, "consume", forbidden_update)

    async def scenario():
        for status in ("approved", "pending", "blocked"):
            with qa_engine.begin() as connection:
                connection.execute(text("UPDATE sources SET enabled=0,permission_status=:status"), {"status": status})
            async with client_for(app_factory) as client:
                result = await state(client)
                assert result["current"] is None
                assert result["remaining"] == 0
        with qa_engine.begin() as connection:
            connection.execute(text("UPDATE sources SET enabled=1,permission_status='approved'"))
        disabled = qa_config.model_copy(deep=True)
        disabled.sources["dataset"].enabled = False
        async with client_for(lambda: app_factory(disabled)) as client:
            assert (await state(client))["current"] is None
    asyncio.run(scenario())
