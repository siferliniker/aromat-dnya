from datetime import date, datetime, timezone

import pytest
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

from storage.database import create_database_engine, initialize_schema
from storage.models import RatingObservation


TABLES = {
    "perfumes",
    "notes",
    "perfume_notes",
    "sources",
    "mentions",
    "bloggers",
    "shop_offers",
    "user_feedback",
    "sent_log",
    "taste_profile",
}


@pytest.fixture
def engine(tmp_path):
    database = tmp_path / "stage-zero.sqlite3"
    engine = create_database_engine(f"sqlite:///{database.as_posix()}")
    try:
        initialize_schema(engine)
        initialize_schema(engine)
        assert database.is_file()
        yield engine
    finally:
        engine.dispose()


def test_initialization_creates_empty_tables_with_primary_keys(engine):
    schema = inspect(engine)
    assert TABLES <= set(schema.get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        for table in TABLES:
            assert schema.get_pk_constraint(table)["constrained_columns"], table
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0


@pytest.mark.parametrize(
    ("table", "column", "parent"),
    [
        ("perfume_notes", "perfume_id", "perfumes"),
        ("perfume_notes", "note_id", "notes"),
        ("mentions", "perfume_id", "perfumes"),
        ("mentions", "source_id", "sources"),
        ("shop_offers", "perfume_id", "perfumes"),
        ("shop_offers", "source_id", "sources"),
        ("user_feedback", "perfume_id", "perfumes"),
        ("sent_log", "perfume_id", "perfumes"),
        ("rating_observations", "perfume_id", "perfumes"),
    ],
)
def test_references_link_records_to_their_perfume_and_source(engine, table, column, parent):
    schema = inspect(engine)
    parent_key = schema.get_pk_constraint(parent)["constrained_columns"]
    assert any(
        foreign_key["constrained_columns"] == [column]
        and foreign_key["referred_table"] == parent
        and foreign_key["referred_columns"] == parent_key
        for foreign_key in schema.get_foreign_keys(table)
    )


@pytest.mark.parametrize(
    ("table", "column", "parent"),
    [
        ("sent_log", "score_snapshot_id", "score_snapshots"),
        ("sent_log", "shop_offer_id", "shop_offers"),
        ("score_evidence", "component_id", "score_components"),
        ("score_evidence", "rating_observation_id", "rating_observations"),
        ("score_evidence", "mention_id", "mentions"),
    ],
)
def test_provenance_references_also_match_perfume_id(engine, table, column, parent):
    assert any(
        set(zip(foreign_key["constrained_columns"], foreign_key["referred_columns"]))
        == {(column, "id"), ("perfume_id", "perfume_id")}
        and foreign_key["referred_table"] == parent
        for foreign_key in inspect(engine).get_foreign_keys(table)
    )


def test_score_evidence_traces_catalog_rank_to_its_source_without_inventing_votes(engine):
    observed_at = datetime(2026, 11, 6, tzinfo=timezone.utc)
    source_url = "https://example.test/snapshot"
    with engine.begin() as connection:
        perfume_id = add_perfume(connection, "Тестовый аромат")
        source_id = add_source(connection, "fixture", "dataset")
        revision_id = add_revision(connection, source_id)
        observation_id = connection.execute(insert(RatingObservation).values(
            source_id=source_id,
            source_kind="dataset",
            source_revision_id=revision_id,
            perfume_id=perfume_id,
            external_id="catalog-01",
            source_url=source_url,
            observed_at=observed_at,
            popularity_rank=1,
        )).lastrowid
        snapshot_id = add_score_snapshot(connection, perfume_id)
        component_id = connection.execute(text("""
            INSERT INTO score_components (snapshot_id, perfume_id, name, value, effective_weight)
            VALUES (:snapshot, :perfume, 'hype', 1, -1)
        """), {"snapshot": snapshot_id, "perfume": perfume_id}).lastrowid
        connection.execute(text("""
            INSERT INTO score_evidence (component_id, perfume_id, rating_observation_id)
            VALUES (:component, :perfume, :observation)
        """), {"component": component_id, "perfume": perfume_id, "observation": observation_id})
        evidence = connection.execute(text("""
            SELECT s.key, r.external_id, r.source_url, r.popularity_rank, r.rating_count, r.mean_rating,
                   v.revision_ref, v.artifact_sha256
            FROM score_evidence AS e
            JOIN rating_observations AS r ON r.id = e.rating_observation_id
            JOIN sources AS s ON s.id = r.source_id
            JOIN source_revisions AS v ON v.id = r.source_revision_id
            WHERE e.component_id = :component
        """), {"component": component_id}).one()
        assert tuple(evidence) == (
            "fixture", "catalog-01", source_url, 1, None, None, "fixture-version", bytes(range(32))
        )
        stored_time = connection.execute(select(RatingObservation.observed_at)).scalar_one()
        assert stored_time == observed_at

        other_perfume = add_perfume(connection, "Другой аромат")
        unrelated_observation = connection.execute(insert(RatingObservation).values(
            source_id=source_id,
            source_kind="dataset",
            source_revision_id=revision_id,
            perfume_id=other_perfume,
            external_id="catalog-02",
            observed_at=observed_at,
            popularity_rank=2,
        )).lastrowid
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(text("""
                UPDATE score_evidence SET rating_observation_id = :observation
                WHERE component_id = :component
            """), {"component": component_id, "observation": unrelated_observation})


def add_source(connection, key, kind):
    connection.execute(text("""
        INSERT INTO sources (key, kind, name) VALUES (:key, :kind, :key)
        ON CONFLICT (key) DO NOTHING
    """), {"key": key, "kind": kind})
    return connection.execute(text("SELECT id FROM sources WHERE key = :key"), {"key": key}).scalar_one()


def add_revision(connection, source_id, digest=bytes(range(32))):
    return connection.execute(text("""
        INSERT INTO source_revisions (source_id, revision_ref, artifact_sha256, artifact_url, approval_basis)
        VALUES (:source, 'fixture-version', :digest, 'https://example.test/snapshot', 'synthetic fixture')
    """), {"source": source_id, "digest": digest}).lastrowid


def add_perfume(connection, name):
    source_id = add_source(connection, "manual", "manual")
    return connection.execute(text("""
        INSERT INTO perfumes (name, house, source_id, source_kind, external_id)
        VALUES (:name, 'Тестовый дом', :source, 'manual', :name)
    """), {"name": name, "source": source_id}).lastrowid


def add_score_snapshot(connection, perfume_id):
    return connection.execute(text("""
        INSERT INTO score_snapshots (perfume_id, computed_at, algorithm_version, scoring_config)
        VALUES (:perfume, CURRENT_TIMESTAMP, 'fixture', '{}')
    """), {"perfume": perfume_id}).lastrowid


def add_delivery(connection, user_id, perfume_id, local_date):
    connection.execute(
        text("""
            INSERT INTO sent_log (user_id, perfume_id, local_date, status)
            VALUES (:user_id, :perfume_id, :local_date, 'reserved')
        """),
        {"user_id": user_id, "perfume_id": perfume_id, "local_date": local_date.isoformat()},
    )


def test_delivery_uniqueness_enforces_today_and_scheduler_contract(engine):
    first_day = date(2026, 11, 6)
    second_day = date(2026, 11, 7)
    with engine.begin() as connection:
        first = add_perfume(connection, "Аромат A")
        second = add_perfume(connection, "Аромат B")
        add_delivery(connection, 10, first, first_day)

    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        with engine.begin() as connection:
            add_delivery(connection, 10, second, first_day)
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        with engine.begin() as connection:
            add_delivery(connection, 10, first, second_day)

    with engine.begin() as connection:
        add_delivery(connection, 20, first, first_day)
        add_delivery(connection, 10, second, second_day)
        assert connection.execute(text("SELECT COUNT(*) FROM sent_log")).scalar_one() == 3


def test_delivery_rejects_nonexistent_perfume(engine):
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        with engine.begin() as connection:
            add_delivery(connection, 10, 999, date(2026, 11, 6))


def test_catalog_rejects_a_perfume_without_source(engine):
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text("""
                INSERT INTO perfumes (name, house, release_year)
                VALUES ('Неподтверждённый аромат', 'Тестовый дом', 2026)
            """))


def test_catalog_requires_a_validated_revision_from_the_same_dataset_source(engine):
    with engine.begin() as connection:
        source_id = add_source(connection, "catalog", "dataset")
        unrelated_source_id = add_source(connection, "other-catalog", "dataset")
        with pytest.raises(IntegrityError, match="CHECK constraint failed"):
            add_revision(connection, source_id, bytes(range(31)))
        revision_id = add_revision(connection, source_id)
        unrelated_revision_id = add_revision(connection, unrelated_source_id)
        perfume = text("""
            INSERT INTO perfumes (name, house, source_id, source_kind, external_id, source_revision_id)
            VALUES ('Аромат', 'Тестовый дом', :source, 'dataset', 'aroma-01', :revision)
        """)

        with pytest.raises(IntegrityError, match="CHECK constraint failed"):
            connection.execute(perfume, {"source": source_id, "revision": None})
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(perfume, {"source": source_id, "revision": unrelated_revision_id})
        connection.execute(perfume, {"source": source_id, "revision": revision_id})


def test_delivery_cannot_reference_the_score_of_another_perfume(engine):
    with engine.begin() as connection:
        first = add_perfume(connection, "Аромат для карточки")
        second = add_perfume(connection, "Другой аромат")
        add_delivery(connection, 10, first, date(2026, 11, 6))
        correct_snapshot = add_score_snapshot(connection, first)
        unrelated_snapshot = add_score_snapshot(connection, second)
        update = text("UPDATE sent_log SET score_snapshot_id = :snapshot")
        connection.execute(update, {"snapshot": correct_snapshot})

        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(update, {"snapshot": unrelated_snapshot})


def test_delivery_cannot_reference_the_shop_offer_of_another_perfume(engine):
    with engine.begin() as connection:
        first = add_perfume(connection, "Аромат для карточки")
        second = add_perfume(connection, "Другой аромат")
        source_id = add_source(connection, "shop", "shop")
        offer = text("""
            INSERT INTO shop_offers (perfume_id, source_id, external_id, source_url, observed_at)
            VALUES (:perfume, :source, :external_id, 'https://example.test/', CURRENT_TIMESTAMP)
        """)
        add_delivery(connection, 10, first, date(2026, 11, 6))
        correct_offer = connection.execute(offer, {
            "perfume": first, "source": source_id, "external_id": "offer-01",
        }).lastrowid
        unrelated_offer = connection.execute(offer, {
            "perfume": second, "source": source_id, "external_id": "offer-02",
        }).lastrowid
        update = text("UPDATE sent_log SET shop_offer_id = :offer")
        connection.execute(update, {"offer": correct_offer})

        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(update, {"offer": unrelated_offer})


def test_confirmed_delivery_requires_timestamp_and_telegram_message_id(engine):
    with engine.begin() as connection:
        perfume_id = add_perfume(connection, "Аромат C")
        add_delivery(connection, 10, perfume_id, date(2026, 11, 6))

    for assignment in ("status = 'sent'", "status = 'sent', sent_at = CURRENT_TIMESTAMP"):
        with pytest.raises(IntegrityError, match="CHECK constraint failed"):
            with engine.begin() as connection:
                connection.execute(text(f"UPDATE sent_log SET {assignment}"))

    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE sent_log
            SET status = 'sent', sent_at = CURRENT_TIMESTAMP, telegram_message_id = 42
        """))
        assert connection.execute(text("SELECT status FROM sent_log")).scalar_one() == "sent"
