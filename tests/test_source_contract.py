import asyncio
import logging
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from collectors.contracts import RatingRecord, SourceBatch, collect_sources


def rating_record(**changes):
    values = {
        "external_id": "rating-01",
        "perfume_external_id": "perfume-01",
        "source_url": "https://example.test/records/rating-01",
        "observed_at": datetime(2026, 11, 6, tzinfo=timezone.utc),
    }
    values.update(changes)
    return RatingRecord.model_validate(values)


def test_source_batch_preserves_rating_provenance_and_missing_vs_zero():
    record = rating_record(rating_count=0, positive_count=0)
    batch = SourceBatch(source_key="fixture", ratings=[record])
    restored = SourceBatch.model_validate_json(batch.model_dump_json())
    assert restored.source_key == "fixture"
    assert len(restored.ratings) == 1
    rating = restored.ratings[0]
    assert rating.external_id == "rating-01"
    assert rating.perfume_external_id == "perfume-01"
    assert rating.source_url == "https://example.test/records/rating-01"
    assert rating.observed_at == datetime(2026, 11, 6, tzinfo=timezone.utc)
    assert rating.mean_rating is None
    assert rating.negative_count is None
    assert rating.rating_count == 0
    assert rating.positive_count == 0


def test_source_batch_rejects_unrecognized_fields():
    with pytest.raises(ValidationError):
        SourceBatch.model_validate({"source_key": "fixture", "unexpected": True})


def test_catalog_popularity_rank_is_a_positive_ordinal_and_does_not_supply_a_vote_count():
    record = rating_record(popularity_rank=1)
    assert record.popularity_rank == 1
    assert record.rating_count is None
    assert record.mean_rating is None


def test_dataset_batch_preserves_revision_identifier_and_rejects_invalid_digest():
    revision = {
        "revision_ref": "fixture-version",
        "artifact_url": "https://example.test/snapshot.json?revision=fixture-version",
        "artifact_sha256": bytes(range(32)).hex(),
    }
    payload = {"source_key": "dataset", "dataset_revision": revision}
    batch = SourceBatch.model_validate(payload)
    restored = SourceBatch.model_validate_json(batch.model_dump_json())
    assert restored.dataset_revision.model_dump() == revision

    payload["dataset_revision"] = revision | {"artifact_sha256": "ab" * 31}
    with pytest.raises(ValidationError):
        SourceBatch.model_validate(payload)


@pytest.mark.parametrize(
    "changes",
    [{"rating_count": -1}, {"positive_count": -1}, {"popularity_rank": 0}, {"unexpected": True}],
)
def test_rating_record_rejects_invalid_data(changes):
    with pytest.raises(ValidationError):
        rating_record(**changes)


@pytest.mark.parametrize(
    "url",
    [
        "https://:bad/",
        "https://example.test/not valid",
        "https://example.test/path\nspoofed",
        "https://fixture-user:fixture-value@example.test/path",
        "https://fixture-user@example.test/path",
    ],
)
def test_rating_record_rejects_malformed_urls_and_urls_with_user_info(url):
    with pytest.raises(ValidationError):
        rating_record(source_url=url)


class FakeAdapter:
    def __init__(self, source_key, result, called):
        self.source_key = source_key
        self.result = result
        self.called = called

    async def collect(self):
        self.called.append(self.source_key)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_collect_sources_continues_after_adapter_error_and_logs_a_safe_reason(caplog):
    called = []
    private_exception_text = "SYNTHETIC_EXCEPTION_CONTENT"
    good_batch = SourceBatch(source_key="healthy", ratings=[rating_record()])
    adapters = [
        FakeAdapter("unhealthy", RuntimeError(private_exception_text), called),
        FakeAdapter("healthy", good_batch, called),
    ]
    with caplog.at_level(logging.WARNING, logger="collectors.contracts"):
        failure, success = asyncio.run(collect_sources(adapters))

    assert called == ["unhealthy", "healthy"]
    assert (failure.source_key, failure.status, failure.reason) == (
        "unhealthy", "failed", "adapter_error"
    )
    assert (success.source_key, success.status, success.batch) == ("healthy", "ok", good_batch)
    assert any(
        record.name == "collectors.contracts" and "adapter_error" in record.getMessage()
        for record in caplog.records
    )
    assert private_exception_text not in caplog.text
    assert private_exception_text not in repr(failure)


def test_invalid_source_key_does_not_block_the_next_adapter():
    called = []
    good_batch = SourceBatch(source_key="healthy")
    adapters = [
        FakeAdapter("invalid source name", SourceBatch(source_key="fixture"), called),
        FakeAdapter("healthy", good_batch, called),
    ]
    failure, success = asyncio.run(collect_sources(adapters))
    assert called == ["healthy"]
    assert (failure.source_key, failure.status, failure.reason) == (None, "failed", "invalid_payload")
    assert (success.source_key, success.status, success.batch) == ("healthy", "ok", good_batch)


@pytest.mark.parametrize(
    "payload",
    [SourceBatch(source_key="different"), {"source_key": "fixture", "ratings": [{}]}],
)
def test_collect_sources_rejects_invalid_adapter_payload(payload):
    failure, = asyncio.run(collect_sources([FakeAdapter("fixture", payload, [])]))
    assert (failure.source_key, failure.status, failure.reason) == (
        "fixture", "failed", "invalid_payload"
    )
