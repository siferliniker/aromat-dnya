"""Independent repository boundaries; all content and approvals are synthetic."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from collectors.contracts import RatingRecord
from collectors.permissions import AdmissionRegistry, SourceAdmission
from llm.contracts import ReviewExtraction
from llm.validation import CatalogPerfume, validate_extraction
from scoring.engine import ScoringInputs, score_opinions
from storage.models import Mention, Perfume, RatingObservation, ScoreComponent, ScoreEvidence, ScoreSnapshot, SentLog, Source, SourceRevision
from storage.opinions import revoke_source
from test_stage2_data import (
    APPROVED, NOW, OP_NAMES, OTHER_URL, QUOTE, URL, admission, data_config, mention,
    repository, review, stored_review,
)


def test_latest_document_is_selected_before_filtering_by_requested_perfume(qa_engine, qa_seed, qa_config):
    first_perfume, second_perfume = qa_seed(2)
    repo = repository(qa_engine, qa_config)
    stored_review(repo, observed_at=NOW - timedelta(seconds=30))
    new_quote = "QA perfume 2 от QA house: нравится свежий аккорд"
    stored_review(
        repo, record=review(text=new_quote),
        mentions=[mention(perfume_external_id="qa-2", evidence_quote=new_quote)],
    )
    assert repo.load_evidence(first_perfume, now=NOW).russian_mentions == ()
    assert repo.load_summary_evidence(first_perfume, now=NOW) == ()
    current = repo.load_evidence(second_perfume, now=NOW)
    assert isinstance(current, ScoringInputs)
    assert len(current.russian_mentions) == 1
    with qa_engine.connect() as connection:
        assert len(connection.execute(select(Mention.id)).all()) == 2


def test_literal_quote_for_a_different_catalog_perfume_cannot_ground_selected_id(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(2)[0]
    repo = repository(qa_engine, qa_config)
    unrelated = "QA perfume 2 от QA house: нравится древесный аккорд"
    result = stored_review(repo, record=review(text=unrelated), mentions=[mention(evidence_quote=unrelated)])
    assert result.imported_mentions == 0
    assert result.quarantined_mentions == 1
    assert repo.load_evidence(perfume_id, now=NOW).russian_mentions == ()


def test_unique_catalog_name_without_stated_house_survives_repository_revalidation(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    quote = "QA perfume 1: a pleasant cedar scent"
    record = review(text=quote)
    raw = ReviewExtraction.model_validate({"mentions": [{
        "perfume_name": "QA perfume 1", "perfume_house": None, "sentiment": "positive",
        "evidence_quote": quote, "confidence": 0.9,
    }]})
    checked = validate_extraction(raw, review=record, catalog=[
        CatalogPerfume(perfume_id, "QA perfume 1", "QA house", "dataset", "qa-1"),
    ])
    assert len(checked.mentions) == 1 and checked.quarantine == ()
    assert checked.mentions[0].perfume_id == perfume_id
    result = stored_review(repo, record=record, mentions=[mention(evidence_quote=quote)])
    assert result.imported_mentions == 1
    assert result.quarantined_mentions == 0
    assert len(repo.load_evidence(perfume_id, now=NOW).russian_mentions) == 1
    assert repo.load_summary_evidence(perfume_id, now=NOW)[0].quote == quote


@pytest.mark.parametrize("stated_house,selected", [
    ("QA house", 0), ("Other house", 1), (None, None),
], ids=["first-house-only", "second-house-only", "ambiguous-without-house"])
def test_same_canonical_name_requires_actual_house_to_select_one_identity(
    qa_engine, qa_seed, qa_config, stated_house, selected,
):
    perfume_ids = qa_seed(2)
    with qa_engine.begin() as connection:
        connection.execute(update(Perfume).where(Perfume.id == perfume_ids[1]).values(
            name="QA  PERFUME 1", house="Other house"))
    repo = repository(qa_engine, qa_config)
    quote = "QA perfume 1: a pleasant cedar scent"
    actual_text = quote if stated_house is None else f"House: {stated_house}. {quote}"
    raw = ReviewExtraction.model_validate({"mentions": [{
        "perfume_name": "QA perfume 1", "perfume_house": stated_house, "sentiment": "positive",
        "evidence_quote": quote, "confidence": 0.9,
    }]})
    catalog = [CatalogPerfume(perfume_ids[0], "QA perfume 1", "QA house", "dataset", "qa-1"),
               CatalogPerfume(perfume_ids[1], "QA  PERFUME 1", "Other house", "dataset", "qa-2")]
    checked = validate_extraction(raw, review=review(text=actual_text), catalog=catalog)
    if selected is None:
        assert checked.mentions == () and checked.quarantine[0].reason == "ambiguous_perfume"
    else:
        assert [item.perfume_id for item in checked.mentions] == [perfume_ids[selected]]
    for index, perfume_id in enumerate(perfume_ids):
        result = stored_review(repo, record=review(external_id=f"identity-{index}", text=actual_text),
            mentions=[mention(perfume_external_id=f"qa-{index + 1}", evidence_quote=quote)])
        assert result.imported_mentions == int(index == selected)
        assert result.quarantined_mentions == int(index != selected)
        assert len(repo.load_evidence(perfume_id, now=NOW).russian_mentions) == int(index == selected)


def test_quote_cannot_ground_a_catalog_name_inside_a_larger_word(qa_engine, qa_seed, qa_config):
    qa_seed(1)
    repo = repository(qa_engine, qa_config)
    unrelated = "QA perfume 10 от QA house: нравится древесный аккорд"
    result = stored_review(repo, record=review(text=unrelated), mentions=[mention(evidence_quote=unrelated)])
    assert result.imported_mentions == 0
    assert result.quarantined_mentions == 1


@pytest.mark.parametrize("field,value", [
    ("confidence", 0.8), ("evidence_quote", "QA perfume 1"),
    ("published_at", NOW - timedelta(days=3)),
])
def test_same_snapshot_cannot_mutate_mention_provenance(qa_engine, qa_seed, qa_config, field, value):
    qa_seed(1)
    repo = repository(qa_engine, qa_config)
    first = stored_review(repo)
    with pytest.raises(ValueError):
        stored_review(repo, mentions=[mention(**{field: value})])
    assert stored_review(repo).revision_id == first.revision_id


def test_snapshot_cannot_rewrite_publication_date_without_new_observation(qa_engine, qa_seed, qa_config):
    qa_seed(1)
    repo = repository(qa_engine, qa_config)
    first = stored_review(repo)
    with pytest.raises(ValueError):
        stored_review(repo, record=review(published_at=NOW - timedelta(days=2)))
    assert stored_review(repo).revision_id == first.revision_id


def test_quotation_right_is_independent_from_derived_score_right(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    repo.registry = AdmissionRegistry([admission(operations={name: name != "quote" for name in OP_NAMES})])
    assert len(repo.load_evidence(perfume_id, now=NOW).russian_mentions) == 1
    assert repo.load_summary_evidence(perfume_id, now=NOW) == ()


def test_expired_or_stale_inputs_never_reappear_through_repository_reads(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    later = NOW + timedelta(seconds=3601)
    assert repo.load_evidence(perfume_id, now=later).russian_mentions == ()
    assert repo.load_summary_evidence(perfume_id, now=later) == ()


def test_revoke_removes_entire_mixed_snapshot_but_preserves_other_source_inputs(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    repo = repository(qa_engine, qa_config, policies=[admission(), admission("youtube")])
    stored_review(repo)
    stored_review(
        repo, source_key="youtube", record=review(blogger_external_id="qa-author"),
        mentions=[mention(blogger_external_id="qa-author")],
    )
    inputs = repo.load_evidence(perfume_id, now=NOW)
    assert len(inputs.russian_mentions) == len(inputs.blogger_mentions) == 1
    score = score_opinions(inputs, data_config(qa_config).scoring)
    snapshot_id = repo.save_score(perfume_id, score, computed_at=NOW)
    with qa_engine.begin() as connection:
        connection.execute(insert(SentLog).values(
            user_id=42, perfume_id=perfume_id, local_date=NOW.date(), status="sent",
            score_snapshot_id=snapshot_id, sent_at=NOW, telegram_message_id=7,
        ))
        other_source_mention_id = inputs.blogger_mentions[0].id
        prior_revisions = connection.execute(select(SourceRevision.id)).scalars().all()
    revoke_source(repo, "otzovik", lambda: None)
    current = repo.load_evidence(perfume_id, now=NOW)
    assert current.russian_mentions == ()
    assert [item.id for item in current.blogger_mentions] == [other_source_mention_id]
    with qa_engine.connect() as connection:
        assert connection.execute(select(ScoreSnapshot.id).where(ScoreSnapshot.id == snapshot_id)).all() == []
        assert connection.execute(select(ScoreComponent.id).where(ScoreComponent.snapshot_id == snapshot_id)).all() == []
        assert connection.execute(select(ScoreEvidence.id)).all() == []
        row = connection.execute(select(
            SentLog.user_id, SentLog.perfume_id, SentLog.status,
            SentLog.telegram_message_id, SentLog.score_snapshot_id,
        )).one()
        assert tuple(row) == (42, perfume_id, "sent", 7, None)
        assert connection.execute(select(Mention.id)).scalars().all() == [other_source_mention_id]
        assert 0 < len(connection.execute(select(SourceRevision.id)).all()) < len(prior_revisions)


def test_revocation_closes_local_llm_gate_before_database_purge_can_fail(qa_engine, qa_seed, qa_config, monkeypatch):
    qa_seed(1)
    repo = repository(qa_engine, qa_config)
    stored_review(repo)
    events = []
    def local_purge():
        with qa_engine.connect() as connection:
            state = connection.execute(select(Source.enabled, Source.permission_status).where(Source.key == "otzovik")).one()
            assert tuple(state) == (False, "blocked")
        events.append("llm-disabled-and-purged")
    def broken_database_purge(source_key):
        events.append("database-purge")
        raise RuntimeError("Synthetic database purge failure")
    monkeypatch.setattr(repo, "purge_source", broken_database_purge)
    with pytest.raises(RuntimeError):
        revoke_source(repo, "otzovik", local_purge)
    assert events == ["llm-disabled-and-purged", "database-purge"]


def test_registry_cannot_be_granted_fetch_by_mutating_original_policy():
    policy = admission(operations={name: name != "fetch" for name in OP_NAMES})
    registry = AdmissionRegistry([policy])
    try:
        policy.operations.fetch = True
    except ValidationError as error:
        assert error.errors()
    with pytest.raises(ValueError):
        registry.require("otzovik", "fetch", now=NOW, source_url=URL)


def test_registry_revalidates_preconstructed_admission_instead_of_trusting_instance_type():
    payload = admission().model_dump(mode="python") | {"platform_approval_ref": None}
    forged = SourceAdmission.model_construct(**payload)
    with pytest.raises((ValueError, ValidationError)):
        AdmissionRegistry([forged]).require("otzovik", "fetch", now=NOW, source_url=URL)


def test_returned_admission_cannot_widen_the_registry_url_allowlist():
    registry = AdmissionRegistry([admission(approved_urls=[URL])])
    policy = registry.require("otzovik", "fetch", now=NOW, source_url=URL)
    if isinstance(policy.approved_urls, list):
        policy.approved_urls.append(OTHER_URL)
    with pytest.raises(ValueError):
        registry.require("otzovik", "fetch", now=NOW, source_url=OTHER_URL)


def test_live_ratings_have_versioned_immutable_approval_lineage(qa_engine, qa_seed, qa_config):
    perfume_id = qa_seed(1)[0]
    policy = admission("fragrantica_live")
    repo = repository(qa_engine, qa_config, policies=[policy])
    record = RatingRecord(
        external_id="qa-live-rating", perfume_external_id="qa-1", perfume_source_key="dataset",
        source_url=URL, observed_at=NOW, mean_rating=4.5, rating_count=1000,
    )
    repo.store_ratings("fragrantica_live", [record], approval_ref=policy.approval_ref, approved_at=APPROVED, now=NOW)
    with qa_engine.connect() as connection:
        revision = connection.execute(select(RatingObservation.source_revision_id).where(
            RatingObservation.external_id == "qa-live-rating",
        )).scalar_one()
        assert revision is not None
    assert len(repo.load_evidence(perfume_id, now=NOW).fragrantica_ratings) == 1
    repo.registry = AdmissionRegistry([admission("fragrantica_live", approval_ref="qa-new-approval")])
    repo.sync_admissions(now=NOW)
    assert repo.load_evidence(perfume_id, now=NOW).fragrantica_ratings == ()
