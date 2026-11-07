from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
import sqlite3

import pytest
from sqlalchemy import text

from storage.repository import RecommendationRepository
from storage.models import ScoreSnapshot
from sqlalchemy.exc import IntegrityError
import storage.repository as repository_module


DAY = date(2026, 11, 7)
NOW = datetime(2026, 11, 7, 4, tzinfo=timezone.utc)
OWNER = 777000123


@pytest.fixture
def repository(qa_engine, qa_config):
    return RecommendationRepository(qa_engine, qa_config.scoring)


def rows(engine, sql):
    with engine.connect() as connection:
        return connection.execute(text(sql)).mappings().all()


def test_empty_catalog_has_no_random_fallback_and_creates_no_reservation(repository, qa_engine):
    assert repository.reserve(OWNER, DAY).status == "empty"
    assert rows(qa_engine, "SELECT * FROM sent_log") == []


@pytest.mark.parametrize("permission_status", ["approved", "pending", "blocked"])
def test_disabled_or_unapproved_source_cannot_supply_recommendations(repository, qa_seed, qa_engine, permission_status):
    qa_seed()
    with qa_engine.begin() as connection:
        connection.execute(text("""
            UPDATE sources SET enabled=0, permission_status=:status
        """), {"status": permission_status})
    assert repository.reserve(OWNER, DAY).status == "empty"
    assert rows(qa_engine, "SELECT * FROM sent_log") == []


def test_reservation_is_committed_before_delivery_and_survives_a_restart(repository, qa_seed, qa_engine, qa_config):
    qa_seed()
    reserved = repository.reserve(OWNER, DAY)
    assert reserved.status == "new"
    persisted = rows(qa_engine, "SELECT * FROM sent_log")
    assert len(persisted) == 1
    assert persisted[0]["status"] == "reserved"
    assert persisted[0]["id"] == reserved.reservation_id
    restarted = RecommendationRepository(qa_engine, qa_config.scoring)
    assert restarted.reserve(OWNER, DAY).status == "reserved"
    assert len(rows(qa_engine, "SELECT * FROM sent_log")) == 1


def test_confirmation_is_compare_and_swap_and_sent_perfume_never_repeats(repository, qa_seed, qa_engine):
    qa_seed()
    first = repository.reserve(OWNER, DAY)
    assert not repository.confirm(first.reservation_id, OWNER + 1, 101, NOW)
    assert repository.confirm(first.reservation_id, OWNER, 101, NOW)
    assert not repository.confirm(first.reservation_id, OWNER, 102, NOW)
    assert not repository.release(first.reservation_id, OWNER)
    assert not repository.mark_uncertain(first.reservation_id, OWNER)
    assert repository.reserve(OWNER, DAY).status == "sent"
    second = repository.reserve(OWNER, DAY + timedelta(days=1))
    assert second.status == "new"
    sent = rows(qa_engine, "SELECT * FROM sent_log ORDER BY id")
    assert sent[0]["telegram_message_id"] == 101
    assert sent[0]["perfume_id"] != sent[1]["perfume_id"]
    assert sent[0]["score_snapshot_id"] is not None


def test_release_cannot_touch_another_or_a_later_reservation(repository, qa_seed, qa_engine):
    qa_seed()
    first = repository.reserve(OWNER, DAY)
    assert not repository.release(first.reservation_id, OWNER + 1)
    assert repository.release(first.reservation_id, OWNER)
    replacement = repository.reserve(OWNER, DAY)
    assert replacement.reservation_id > first.reservation_id
    assert not repository.release(first.reservation_id, OWNER)
    assert not repository.confirm(first.reservation_id, OWNER, 200, NOW)
    assert not repository.mark_uncertain(first.reservation_id, OWNER)
    assert rows(qa_engine, "SELECT * FROM sent_log")[0]["id"] == replacement.reservation_id


def test_uncertain_outcome_blocks_retries_and_cannot_be_released(repository, qa_seed, qa_engine):
    qa_seed()
    result = repository.reserve(OWNER, DAY)
    assert repository.mark_uncertain(result.reservation_id, OWNER)
    assert not repository.mark_uncertain(result.reservation_id, OWNER)
    assert not repository.release(result.reservation_id, OWNER)
    assert not repository.confirm(result.reservation_id, OWNER, 300, NOW)
    assert repository.reserve(OWNER, DAY).status == "uncertain"
    assert len(rows(qa_engine, "SELECT * FROM sent_log")) == 1


@pytest.mark.parametrize("reaction", ["known", "bought", "not_for_me"])
def test_candidate_filter_excludes_feedback_even_when_never_sent(repository, qa_seed, qa_engine, reaction):
    ids = qa_seed(1)
    with qa_engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO user_feedback(user_id,perfume_id,reaction) VALUES (:user,:perfume,:reaction)
        """), {"user": OWNER, "perfume": ids[0], "reaction": reaction})
    assert repository.reserve(OWNER, DAY).status == "empty"
    assert not rows(qa_engine, "SELECT * FROM sent_log")


def test_feedback_requires_the_same_sent_message_and_upserts_stably(repository, qa_seed, qa_engine):
    ids = qa_seed(2)
    reserved = repository.reserve(OWNER, DAY)
    perfume_id = rows(qa_engine, "SELECT perfume_id FROM sent_log")[0]["perfume_id"]
    other_id = next(value for value in ids if value != perfume_id)
    assert not repository.feedback(OWNER, perfume_id, "want", 300)
    assert repository.confirm(reserved.reservation_id, OWNER, 300, NOW)
    for user, perfume, message in [(OWNER + 1, perfume_id, 300), (OWNER, other_id, 300), (OWNER, perfume_id, 301)]:
        assert not repository.feedback(user, perfume, "want", message)
    assert repository.feedback(OWNER, perfume_id, "want", 300)
    assert repository.feedback(OWNER, perfume_id, "want", 300)
    assert len(repository.list_feedback(OWNER, ["want"])) == 1
    assert repository.feedback(OWNER, perfume_id, "bought", 300)
    feedback = rows(qa_engine, "SELECT * FROM user_feedback")
    assert len(feedback) == 1
    assert feedback[0]["reaction"] == "bought"
    assert repository.list_feedback(OWNER, ["want"]) == []
    assert len(repository.list_feedback(OWNER, ["known", "bought"])) == 1
    with pytest.raises(ValueError):
        repository.feedback(OWNER, perfume_id, "invented", 300)


def test_concurrent_independent_repositories_create_one_daily_reservation(qa_engine, qa_config, qa_seed):
    qa_seed(8)
    barrier = Barrier(4)
    def reserve(_):
        instance = RecommendationRepository(qa_engine, qa_config.scoring)
        barrier.wait(timeout=10)
        return instance.reserve(OWNER, DAY)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert [result.status for result in results].count("new") == 1
    assert {result.status for result in results} <= {"new", "reserved"}
    assert len(rows(qa_engine, "SELECT * FROM sent_log")) == 1


def test_parallel_dates_cannot_reserve_the_same_perfume_twice(qa_engine, qa_config, qa_seed):
    qa_seed(8)
    barrier = Barrier(4)
    def reserve(offset):
        instance = RecommendationRepository(qa_engine, qa_config.scoring)
        barrier.wait(timeout=10)
        return instance.reserve(OWNER, DAY + timedelta(days=offset))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert [result.status for result in results] == ["new"] * 4
    persisted = rows(qa_engine, "SELECT * FROM sent_log")
    assert len(persisted) == len({row["perfume_id"] for row in persisted}) == 4
    assert len({row["local_date"] for row in persisted}) == 4


def test_catalogue_without_usable_numeric_evidence_is_empty_without_fallback(repository, qa_seed, qa_engine):
    qa_seed(1)
    with qa_engine.begin() as connection:
        connection.execute(text("UPDATE rating_observations SET popularity_rank=NULL"))
    assert repository.reserve(OWNER, DAY).status == "empty"
    assert not rows(qa_engine, "SELECT * FROM sent_log")


def test_delete_removes_only_target_user_data_and_preserves_catalog(repository, qa_seed, qa_engine):
    qa_seed()
    for user in [OWNER, OWNER + 1]:
        delivery = repository.reserve(user, DAY)
        assert repository.confirm(delivery.reservation_id, user, 300 + user, NOW)
        perfume = rows(qa_engine, f"SELECT perfume_id FROM sent_log WHERE user_id={user}")[0]["perfume_id"]
        assert repository.feedback(user, perfume, "want", 300 + user)
    repository.delete_user_data(OWNER)
    assert not repository.list_feedback(OWNER, ["want", "known", "bought", "not_for_me"])
    assert {row["user_id"] for row in rows(qa_engine, "SELECT * FROM sent_log")} == {OWNER + 1}
    assert {row["user_id"] for row in rows(qa_engine, "SELECT * FROM user_feedback")} == {OWNER + 1}
    assert len(rows(qa_engine, "SELECT * FROM perfumes")) == 4


def test_permanent_integrity_failure_is_propagated_without_retrying_forever(repository, qa_seed, qa_engine, monkeypatch):
    qa_seed()
    initial = repository.reserve(OWNER, DAY)
    assert repository.confirm(initial.reservation_id, OWNER, 123, NOW)
    original_session = repository_module.Session
    attempts = []
    class RejectSnapshotSession(original_session):
        def flush(self, *args, **kwargs):
            if any(isinstance(instance, ScoreSnapshot) for instance in self.new):
                attempts.append(True)
                if len(attempts) > 2:
                    raise RuntimeError("Permanent integrity failure was retried without a bound")
                raise IntegrityError("INSERT INTO score_snapshots", {}, sqlite3.IntegrityError("CHECK constraint failed: qa_permanent"))
            return super().flush(*args, **kwargs)
    monkeypatch.setattr(repository_module, "Session", RejectSnapshotSession)
    with pytest.raises(IntegrityError, match="qa_permanent"):
        repository.reserve(OWNER, DAY + timedelta(days=1))
    assert len(attempts) == 1
    assert len(rows(qa_engine, "SELECT * FROM sent_log")) == 1
