from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from collectors.contracts import DatasetRevisionRecord, NoteRecord, PerfumeRecord, RatingRecord, SourceBatch
from storage.repository import DatasetApproval, DatasetRepository


REVISION = DatasetRevisionRecord(
    revision_ref="qa-synthetic-v1", artifact_url="https://example.test/approved.jsonl",
    artifact_sha256="12" * 32,
)


@pytest.fixture
def approval():
    return DatasetApproval(
        name="QA synthetic dataset", terms_url="https://creativecommons.org/licenses/by/4.0/",
        access_basis="Synthetic test data", approval_basis="QA-only fixture",
        approved_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
        revision_ref=REVISION.revision_ref, artifact_url=REVISION.artifact_url,
        artifact_sha256=REVISION.artifact_sha256,
    )


@pytest.fixture
def batch():
    return SourceBatch(
        source_key="dataset", dataset_revision=REVISION,
        perfumes=[PerfumeRecord(
            external_id="qa-rose", name="Rose <QA>", house="QA & house",
            source_url="https://example.test/qa-rose",
            notes=[NoteRecord(name="rose", level="unspecified")],
        )],
        ratings=[RatingRecord(
            external_id="qa-rose-rank", perfume_external_id="qa-rose",
            observed_at=datetime(2026, 11, 7, tzinfo=timezone.utc), popularity_rank=750,
        )],
    )


def enabled_config(config):
    value = config.model_copy(deep=True)
    value.sources["dataset"].enabled = True
    return value


def counts(engine):
    with engine.connect() as connection:
        return {name: connection.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one()
                for name in ["sources", "source_revisions", "perfumes", "notes", "perfume_notes", "rating_observations"]}


def test_dataset_import_is_idempotent_and_retains_nulls_notes_original_rank_and_provenance(qa_engine, qa_config, approval, batch):
    repository = DatasetRepository(qa_engine, enabled_config(qa_config))
    first = repository.import_batch(batch, approval)
    initial = counts(qa_engine)
    second = repository.import_batch(batch, approval)
    assert first.total == second.total == 1
    assert counts(qa_engine) == initial
    with qa_engine.connect() as connection:
        perfume = connection.execute(text("SELECT * FROM perfumes")).mappings().one()
        rating = connection.execute(text("SELECT * FROM rating_observations")).mappings().one()
        revision = connection.execute(text("SELECT * FROM source_revisions")).mappings().one()
        assert perfume["external_id"] == "qa-rose"
        assert perfume["release_year"] is perfume["perfumers"] is perfume["photo_url"] is None
        assert perfume["source_url"] == "https://example.test/qa-rose"
        assert perfume["source_revision_id"] == rating["source_revision_id"] == revision["id"]
        assert rating["popularity_rank"] == 750
        assert rating["mean_rating"] is rating["rating_count"] is rating["positive_count"] is rating["negative_count"] is None
        assert bytes(revision["artifact_sha256"]).hex() == REVISION.artifact_sha256
        assert revision["revision_ref"] == REVISION.revision_ref
        assert connection.execute(text("SELECT level FROM perfume_notes")).scalar_one() == "unspecified"


def test_import_with_invalid_cross_record_reference_rolls_back_whole_transaction(qa_engine, qa_config, approval, batch):
    invalid = batch.model_copy(deep=True)
    invalid.ratings[0].perfume_external_id = "does-not-exist"
    before = counts(qa_engine)
    with pytest.raises((ValueError, LookupError)):
        DatasetRepository(qa_engine, enabled_config(qa_config)).import_batch(invalid, approval)
    assert counts(qa_engine) == before


@pytest.mark.parametrize("field,value", [
    ("artifact_sha256", "34" * 32), ("revision_ref", "different-revision"),
    ("artifact_url", "https://example.test/different.jsonl"),
])
def test_unapproved_snapshot_identity_is_rejected_before_writing(qa_engine, qa_config, approval, batch, field, value):
    invalid = batch.model_copy(deep=True)
    setattr(invalid.dataset_revision, field, value)
    with pytest.raises(ValueError):
        DatasetRepository(qa_engine, enabled_config(qa_config)).import_batch(invalid, approval)
    assert not any(counts(qa_engine).values())


def test_disabled_dataset_cannot_be_imported(qa_engine, qa_config, approval, batch):
    disabled = qa_config.model_copy(deep=True)
    disabled.sources["dataset"].enabled = False
    with pytest.raises(ValueError):
        DatasetRepository(qa_engine, disabled).import_batch(batch, approval)
    assert not any(counts(qa_engine).values())


def test_failure_after_first_insert_rolls_back_all_new_catalogue_and_notes(qa_engine, qa_config, approval, batch):
    repository = DatasetRepository(qa_engine, enabled_config(qa_config))
    repository.import_batch(batch, approval)
    before = counts(qa_engine)
    invalid = batch.model_copy(deep=True)
    invalid.perfumes[0].house = "Different data under the same approved hash"
    invalid.perfumes.insert(0, PerfumeRecord(
        external_id="qa-new-before-failure", name="New before failure", house="QA house",
        notes=[NoteRecord(name="QA rollback-only note", level="unspecified")],
    ))
    with pytest.raises(ValueError):
        repository.import_batch(invalid, approval)
    assert counts(qa_engine) == before
    with qa_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM perfumes WHERE external_id='qa-new-before-failure'")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM notes WHERE name='QA rollback-only note'")).scalar_one() == 0


def test_reimport_cannot_append_notes_to_an_existing_record_under_unchanged_snapshot_hash(qa_engine, qa_config, approval, batch):
    repository = DatasetRepository(qa_engine, enabled_config(qa_config))
    repository.import_batch(batch, approval)
    before = counts(qa_engine)
    changed = batch.model_copy(deep=True)
    changed.perfumes[0].notes.append(NoteRecord(name="QA invented extra note", level="base"))
    with pytest.raises(ValueError):
        repository.import_batch(changed, approval)
    assert counts(qa_engine) == before
