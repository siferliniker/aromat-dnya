"""Synthetic fixtures for independent stage-one acceptance tests."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import insert, text

from config import load_config
from storage.database import create_database_engine, initialize_schema
from storage.models import RatingObservation


@pytest.fixture
def qa_config():
    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


@pytest.fixture
def qa_engine(tmp_path):
    database = tmp_path / "stage-one.sqlite3"
    engine = create_database_engine(f"sqlite:///{database.as_posix()}")
    initialize_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def qa_seed(qa_engine):
    def seed(count=4):
        ids = []
        with qa_engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO sources (key,kind,name,enabled,permission_status,access_basis)
                VALUES ('dataset','dataset','QA fixture',1,'approved','Synthetic QA approval')
                ON CONFLICT (key) DO NOTHING
            """))
            source_id = connection.execute(text(
                "SELECT id FROM sources WHERE key='dataset'"
            )).scalar_one()
            connection.execute(text("""
                INSERT INTO source_revisions(source_id,revision_ref,artifact_sha256,artifact_url,approval_basis)
                VALUES (:source,'qa-synthetic-v1',:digest,'https://example.test/qa.jsonl','Synthetic QA snapshot')
                ON CONFLICT (source_id,revision_ref,artifact_sha256) DO NOTHING
            """), {"source": source_id, "digest": bytes.fromhex("12" * 32)})
            revision_id = connection.execute(text(
                "SELECT id FROM source_revisions WHERE source_id=:source"
            ), {"source": source_id}).scalar_one()
            existing = connection.execute(text("SELECT COUNT(*) FROM perfumes")).scalar_one()
            for offset in range(count):
                number = existing + offset + 1
                perfume_id = connection.execute(text("""
                    INSERT INTO perfumes(name,house,source_id,source_kind,external_id,source_revision_id)
                    VALUES (:name,'QA house',:source,'dataset',:external,:revision)
                """), {
                    "name": f"QA perfume {number}", "source": source_id,
                    "external": f"qa-{number}",
                    "revision": revision_id,
                }).lastrowid
                ids.append(perfume_id)
                connection.execute(insert(RatingObservation).values(
                    perfume_id=perfume_id, source_id=source_id, source_kind="dataset",
                    source_revision_id=revision_id,
                    external_id=f"qa-rank-{number}",
                    observed_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
                    popularity_rank=number * 100,
                ))
        return ids
    return seed
