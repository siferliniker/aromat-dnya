import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from collectors.dataset import DatasetAdapter


DATA = Path(__file__).resolve().parents[1] / "data" / "scento"


def test_bundled_dataset_identity_license_and_300_records_are_real_source_records():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    artifact = DATA / manifest["artifact_file"]
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == manifest["artifact_sha256"]
    assert hashlib.sha256((DATA / manifest["license_file"]).read_bytes()).hexdigest() == manifest["license_sha256"]
    assert manifest["license"] == "CC-BY-4.0"
    assert manifest["revision_ref"] and manifest["approval_basis"] and manifest["approved_at"]
    original = [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines()]
    assert len(original) == manifest["total_records"] == 1479
    by_id = {record["id"]: record for record in original}
    batch = asyncio.run(DatasetAdapter(artifact, DATA / "manifest.json", limit=300).collect())
    assert batch.source_key == "dataset"
    assert 200 <= len(batch.perfumes) <= 500
    assert len(batch.perfumes) == len(batch.ratings) == 300
    assert batch.dataset_revision.artifact_sha256 == manifest["artifact_sha256"]
    assert batch.dataset_revision.revision_ref == manifest["revision_ref"]
    assert {p.external_id for p in batch.perfumes} == set(sorted(by_id)[:300])
    assert len({p.external_id for p in batch.perfumes}) == 300
    for perfume in batch.perfumes:
        raw = by_id[perfume.external_id]
        assert (perfume.name, perfume.house, perfume.source_url) == (raw["name"], raw["house"], raw["url"])
        assert perfume.release_year == raw["year"]
        assert perfume.perfumers == (raw["perfumers"] or None)
        assert perfume.photo_url is None
        assert perfume.notes
        assert set(raw["notes"]) <= {note.name for note in perfume.notes}
    for rating in batch.ratings:
        assert rating.popularity_rank == by_id[rating.perfume_external_id]["popularity_rank"]
        assert rating.mean_rating is rating.rating_count is rating.positive_count is rating.negative_count is None
    assert not batch.mentions and not batch.review_texts and not batch.offers


def test_adapter_rejects_artifact_bytes_that_do_not_match_pinned_hash(tmp_path):
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    corrupted = tmp_path / manifest["artifact_file"]
    corrupted.write_bytes((DATA / manifest["artifact_file"]).read_bytes() + b"\n")
    with pytest.raises(ValueError):
        asyncio.run(DatasetAdapter(corrupted, DATA / "manifest.json", limit=300).collect())
