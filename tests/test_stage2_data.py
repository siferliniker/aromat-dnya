"""Independent offline boundaries for admission, opinion lineage and revocation."""

from datetime import datetime, timedelta, timezone
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select

from collectors.contracts import MentionRecord, RatingRecord, ReviewTextRecord
from collectors.permissions import AdmissionRegistry, Operations, SourceAdmission, load_admission_registry
from scoring.engine import score_opinions
from storage.models import (
    Mention, Perfume, RatingObservation, ScoreEvidence, ScoreSnapshot, SentLog, Source,
    SourceRevision, UserFeedback,
)
from storage.opinions import OpinionRepository, revoke_source


NOW = datetime(2026, 11, 8, 6, tzinfo=timezone.utc)
APPROVED = NOW - timedelta(days=1)
URL = "https://example.test/reviews/qa-document"
OTHER_URL = "https://example.test/reviews/other-document"
QUOTE = "QA perfume 1 от QA house: нравится древесный аккорд"
CANARY = "QA_FULL_REVIEW_BODY_NOT_PERMITTED_FOR_LOCAL_RETENTION"
TEXT = f"{CANARY}. {QUOTE}."
OP_NAMES = ("fetch", "store", "llm", "derive", "quote", "golden")


def admission(source_key="otzovik", **changes):
    kind = "blog" if source_key in ("youtube", "telegram") else "manual" if source_key == "manual" else "review"
    role = "bloggers" if kind == "blog" else "popularity" if kind == "manual" else "fragrantica" if source_key == "fragrantica_live" else "russian_reviews"
    values = dict(
        source_key=source_key, kind=kind, name=f"Synthetic QA {source_key}", platform=source_key,
        enabled=True, permission_status="approved", scope="manual_nonprivate" if kind == "manual" else "public_review",
        terms_url="https://example.test/terms", approval_ref=f"qa-approval-{source_key}",
        platform_approval_ref="qa-platform-attestation", author_approval_ref="qa-author-attestation",
        approved_at=APPROVED, expires_at=NOW + timedelta(days=1), max_age_seconds=3600,
        operations={name: True for name in OP_NAMES}, approved_urls=[URL, OTHER_URL],
        bloggers=([dict(external_id="qa-author", name="QA Author", channel_url="https://example.test/channel/qa", weight=0.75)] if kind == "blog" else []),
        evidence_role=role,
    ) | changes
    return SourceAdmission.model_validate(values)


def data_config(qa_config):
    values = qa_config.model_dump(mode="python")
    for key in ("otzovik", "youtube", "telegram", "fragrantica_live", "manual"):
        values["sources"][key]["enabled"] = True
    values["llm"] = {"provider": "openai", "enabled": True, "model": "qa-offline-model"}
    values["stage2"] = {
        "enabled": True, "approvals_path": "qa-synthetic-admissions.json", "claims_path": "qa-synthetic-claims",
        "freshness_seconds": {key: 3600 for key in ("otzovik", "youtube", "telegram", "fragrantica_live")},
        "fetch": {"request_timeout_seconds": 2.0, "source_deadline_seconds": 5.0, "max_attempts": 2,
                  "retry_backoff_seconds": [0.0], "min_request_interval_seconds": 0.0, "max_items_per_source": 5},
        "llm": {"request_timeout_seconds": 2.0, "max_attempts": 1, "retry_backoff_seconds": [],
                "max_input_chars": 4000, "max_output_tokens": 400, "max_requests_per_run": 10,
                "max_concurrent_requests": 2},
    }
    return type(qa_config).model_validate(values)


def review(**changes):
    return ReviewTextRecord(**(dict(external_id="qa-document", source_url=URL, text=TEXT) | changes))


def mention(**changes):
    return MentionRecord(**(dict(
        external_id="qa-mention", perfume_source_key="dataset", perfume_external_id="qa-1",
        source_url=URL, sentiment="positive", evidence_quote=QUOTE, confidence=0.9,
    ) | changes))


def stored_review(repository, *, source_key="otzovik", record=None, mentions=None, observed_at=NOW, **changes):
    return repository.store_review_snapshot(**(dict(
        source_key=source_key, review=record or review(), observed_at=observed_at,
        approval_ref=f"qa-approval-{source_key}", approved_at=APPROVED,
        mentions=[mention()] if mentions is None else mentions, now=NOW,
    ) | changes))


def repository(qa_engine, qa_config, policies=None):
    registry = AdmissionRegistry(policies or [admission()])
    result = OpinionRepository(qa_engine, data_config(qa_config), registry)
    result.sync_admissions(now=NOW)
    return result


@pytest.mark.parametrize("field", OP_NAMES)
def test_admission_operations_are_explicit_strict_booleans(field):
    with pytest.raises(ValidationError):
        Operations.model_validate({name: True for name in OP_NAMES if name != field})
    with pytest.raises(ValidationError):
        Operations.model_validate({name: True for name in OP_NAMES} | {field: 1})


@pytest.mark.parametrize("changes", [
    {"approval_ref": ""}, {"platform_approval_ref": None}, {"author_approval_ref": None},
    {"approved_at": None}, {"approved_at": APPROVED.replace(tzinfo=None)}, {"approved_urls": []},
])
def test_enabled_approved_source_requires_separate_actual_permission_attestations(changes):
    with pytest.raises((ValidationError, ValueError)):
        admission(**changes)


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"permission_status": "blocked"}, {"expires_at": NOW},
    {"approved_at": NOW + timedelta(seconds=1)}, {"operations": {name: name != "llm" for name in OP_NAMES}},
])
def test_registry_denies_inactive_expired_future_or_ungranted_operation(changes):
    registry = AdmissionRegistry([admission(**changes)])
    with pytest.raises((ValueError, ValidationError)):
        registry.require("otzovik", "llm", now=NOW, source_url=URL)


def test_exact_url_and_explicit_blogger_are_required_without_prefix_or_manual_fallback():
    registry = AdmissionRegistry([admission("youtube")])
    assert registry.require("youtube", "derive", now=NOW, source_url=URL, blogger_external_id="qa-author").source_key == "youtube"
    for changes in ({"source_url": URL + "/unapproved"}, {"blogger_external_id": "unknown-author"}):
        params = dict(source_url=URL, blogger_external_id="qa-author") | changes
        with pytest.raises(ValueError):
            registry.require("youtube", "derive", now=NOW, **params)


def test_llm_approval_preserves_exact_scope_and_rights_without_widening_urls():
    policy = admission(operations={name: name != "quote" for name in OP_NAMES}, approved_urls=[URL])
    mapped = AdmissionRegistry([policy]).to_llm_approval("otzovik", now=NOW)
    assert mapped.approved_urls == (URL,)
    assert mapped.allow_quotation is False
    assert mapped.allow_retention is True
    assert mapped.obtained_at == APPROVED
    assert mapped.max_age_seconds == 3600


def test_admission_json_requires_explicit_sources_and_rejects_duplicate_identity(tmp_path):
    path = tmp_path / "admissions.json"
    policy = admission().model_dump(mode="json")
    path.write_text(json.dumps({"sources": [policy]}), encoding="utf-8")
    assert load_admission_registry(path).require("otzovik", "store", now=NOW, source_url=URL).source_key == "otzovik"
    path.write_text(json.dumps({"sources": [policy, policy]}), encoding="utf-8")
    with pytest.raises((ValueError, ValidationError)):
        load_admission_registry(path)


def test_review_snapshot_is_idempotent_quote_only_and_fetched_time_is_preserved(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    first = stored_review(repo, observed_at=NOW - timedelta(seconds=15))
    second = stored_review(repo, observed_at=NOW - timedelta(seconds=15))
    assert first.revision_id == second.revision_id
    evidence = repo.load_evidence(perfume_id, now=NOW)
    assert len(evidence.russian_mentions) == 1
    assert evidence.russian_mentions[0].observed_at == NOW - timedelta(seconds=15)
    assert evidence.russian_mentions[0].published_at is None
    summaries = repo.load_summary_evidence(perfume_id, now=NOW)
    assert len(summaries) == 1
    assert summaries[0].quote == QUOTE
    assert summaries[0].observed_at == NOW - timedelta(seconds=15)
    with qa_engine.connect() as connection:
        quotes = connection.execute(select(Mention.evidence_quote)).scalars().all()
        metadata = connection.execute(select(SourceRevision.approval_basis)).scalars().all()
        assert quotes == [QUOTE]
        assert CANARY not in json.dumps(metadata)


def test_new_empty_document_revision_suppresses_old_mentions_without_deleting_history(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    old = stored_review(repo, observed_at=NOW - timedelta(seconds=30))
    new = stored_review(repo, record=review(text="В новой версии упоминаний аромата нет."), mentions=[])
    assert old.revision_id != new.revision_id
    assert repo.load_evidence(perfume_id, now=NOW).russian_mentions == ()
    assert repo.load_summary_evidence(perfume_id, now=NOW) == ()
    with qa_engine.connect() as connection:
        assert len(connection.execute(select(Mention.id)).all()) == 1


@pytest.mark.parametrize("changes", [
    {"record": review(text="Другой текст. " + TEXT)},
    {"record": review(source_url=OTHER_URL)},
    {"mentions": [mention(sentiment="negative")]},
])
def test_same_document_same_observation_cannot_mutate_origin_text_or_extraction(qa_engine, qa_seed, qa_config, changes):
    qa_seed(1)
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    with pytest.raises(ValueError):
        stored_review(repo, **changes)


@pytest.mark.parametrize("changes", [
    {"observed_at": NOW.replace(tzinfo=None)}, {"observed_at": NOW + timedelta(seconds=1)},
    {"observed_at": NOW - timedelta(seconds=3601)}, {"approval_ref": "wrong-approval"},
    {"approved_at": APPROVED - timedelta(days=1)}, {"record": review(source_url=URL + "/not-approved")},
])
def test_snapshot_admission_rejects_bad_time_rights_and_exact_origin(qa_engine, qa_seed, qa_config, changes):
    qa_seed(1)
    repo = repository(qa_engine, qa_config)
    with pytest.raises((ValueError, ValidationError)):
        stored_review(repo, **changes)
    with qa_engine.connect() as connection:
        assert connection.execute(select(Mention.id)).all() == []


@pytest.mark.parametrize("changes", [
    {"perfume_external_id": "unknown-id"}, {"evidence_quote": "Цитата отсутствует во входе"},
    {"evidence_quote": CANARY},
])
def test_unmapped_or_ungrounded_mention_is_quarantined_without_creating_perfume(qa_engine, qa_seed, qa_config, changes):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    result = stored_review(repo, mentions=[mention(**changes)])
    assert result.imported_mentions == 0
    assert result.quarantined_mentions == 1
    assert repo.load_evidence(perfume_id, now=NOW).russian_mentions == ()
    with qa_engine.connect() as connection:
        assert len(connection.execute(select(Perfume.id)).all()) == 1


def test_disabled_source_tombstone_cannot_be_revived_by_reloading_old_approval(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    repo.block_source("otzovik")
    repo.sync_admissions(now=NOW)
    with pytest.raises(ValueError):
        stored_review(repo)
    assert repo.load_evidence(perfume_id, now=NOW).russian_mentions == ()
    with qa_engine.connect() as connection:
        stored = connection.execute(select(Source.permission_status, Source.enabled).where(Source.key == "otzovik")).one()
        assert tuple(stored) == ("blocked", False)


def test_score_snapshot_references_and_applied_trust_are_saved_from_current_inputs(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    score = score_opinions(repo.load_evidence(perfume_id, now=NOW), data_config(qa_config).scoring)
    snapshot_id = repo.save_score(perfume_id, score, computed_at=NOW)
    with qa_engine.connect() as connection:
        saved = connection.execute(select(ScoreSnapshot).where(ScoreSnapshot.id == snapshot_id)).one()
        assert saved.perfume_id == perfume_id
        references = connection.execute(select(ScoreEvidence.mention_id).where(ScoreEvidence.mention_id.is_not(None))).scalars().all()
        assert references
        assert set(references) == {item.id for item in repo.load_evidence(perfume_id, now=NOW).russian_mentions}


def test_purge_requires_prior_durable_block(qa_engine, qa_seed, qa_config):
    qa_seed(1)
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    with pytest.raises(ValueError):
        repo.purge_source("otzovik")
    with qa_engine.connect() as connection:
        assert connection.execute(select(Mention.id)).all()


@pytest.mark.parametrize("failure_stage", ["database", "local_llm"])
def test_revoke_partial_failure_keeps_durable_block_and_never_reopens_processing(
    qa_engine, qa_seed, qa_config, monkeypatch, failure_stage,
):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    local_calls = []
    if failure_stage == "database":
        def broken_database(source_key):
            raise RuntimeError("Synthetic purge failure")
        monkeypatch.setattr(repo, "purge_source", broken_database)
    def local_purge():
        local_calls.append(True)
        if failure_stage == "local_llm":
            raise RuntimeError("Synthetic local cache purge failure")
    with pytest.raises(RuntimeError):
        revoke_source(repo, "otzovik", local_purge)
    with qa_engine.connect() as connection:
        state = connection.execute(select(Source.permission_status, Source.enabled).where(Source.key == "otzovik")).one()
        assert tuple(state) == ("blocked", False)
    assert repo.load_evidence(perfume_id, now=NOW).russian_mentions == ()
    repo.sync_admissions(now=NOW)
    with pytest.raises(ValueError):
        stored_review(repo)
    if failure_stage == "local_llm":
        assert local_calls == [True]


def test_revoke_deletes_derived_snapshot_and_preserves_user_and_catalog_history(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    score = score_opinions(repo.load_evidence(perfume_id, now=NOW), data_config(qa_config).scoring)
    snapshot_id = repo.save_score(perfume_id, score, computed_at=NOW)
    with qa_engine.begin() as connection:
        connection.execute(insert(UserFeedback).values(user_id=42, perfume_id=perfume_id, reaction="want"))
        connection.execute(insert(SentLog).values(
            user_id=42, perfume_id=perfume_id, local_date=NOW.date(), status="reserved", score_snapshot_id=snapshot_id,
        ))
    calls = []
    revoke_source(repo, "otzovik", lambda: calls.append(True))
    assert calls == [True]
    with qa_engine.connect() as connection:
        assert connection.execute(select(ScoreSnapshot.id).where(ScoreSnapshot.id == snapshot_id)).all() == []
        assert connection.execute(select(Mention.id)).all() == []
        assert len(connection.execute(select(Perfume.id)).all()) == 1
        assert len(connection.execute(select(UserFeedback.user_id)).all()) == 1
        sent = connection.execute(select(SentLog.user_id, SentLog.perfume_id, SentLog.score_snapshot_id)).one()
        assert tuple(sent) == (42, perfume_id, None)
        assert connection.execute(select(RatingObservation.id)).all()


def test_manual_source_cannot_supply_fragrantica_rating_evidence(qa_engine, qa_seed, qa_config):
    qa_seed(1)
    with pytest.raises((ValueError, ValidationError)):
        repo = repository(qa_engine, qa_config, policies=[admission("manual", evidence_role="fragrantica")])
        repo.store_ratings("manual", [RatingRecord(
            external_id="qa-rating", perfume_external_id="qa-1", perfume_source_key="dataset",
            source_url=URL, observed_at=NOW, mean_rating=5.0, rating_count=1000,
        )], approval_ref="qa-approval-manual", approved_at=APPROVED, now=NOW)
