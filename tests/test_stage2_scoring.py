"""Independent synthetic acceptance tests of the complete stage-two scorer."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import insert, select, text

from scoring.engine import (
    EvidenceRef, MentionEvidence, RatingEvidence, ScoringInputs, score_opinions,
)
from storage.models import (
    Blogger, Mention, RatingObservation, ScoreComponent, ScoreEvidence, ScoreSnapshot, Source,
)


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
NAMES = ("fragrantica", "russian_reviews", "bloggers", "consensus", "novelty", "hype", "taste")


def configuration(qa_config, *, weights=None, policy=None, bayesian=None, thresholds=None):
    values = qa_config.scoring.model_dump(mode="python")
    values["weights"] = dict.fromkeys(NAMES, 0.0) | (weights or {"fragrantica": 1.0})
    values["bayesian"] = {
        "rating_prior_mean": 3.0, "reaction_prior_positive": 0.5, "prior_count": 10,
    } | (bayesian or {})
    values["opinion_components"] = {
        "fragrantica_rating_share": 0.5, "russian_review_prior_mean": 0.5,
        "russian_review_prior_count": 10,
    } | (policy or {})
    values["thresholds"] = {
        "novelty_max_rating_count": 100, "hype_min_rating_count": 1000,
        "hype_max_popularity_rank": 50, "consensus_max_gap": 0.2,
    } | (thresholds or {})
    return type(qa_config.scoring).model_validate(values)


def rating(identifier=1, **changes):
    return RatingEvidence(**(dict(
        id=identifier, source_key="fragrantica", mean_rating=None, rating_count=None,
        positive_count=None, negative_count=None, popularity_rank=None, observed_at=NOW,
    ) | changes))


def mention(identifier=1, **changes):
    return MentionEvidence(**(dict(
        id=identifier, source_key="otzovik", blogger_id=None, sentiment="positive",
        confidence=None, applied_blogger_weight=None, observed_at=NOW, published_at=None,
        document_key=None,
    ) | changes))


def inputs(*, ratings=(), russian=(), bloggers=(), popularity=()):
    return ScoringInputs(
        fragrantica_ratings=ratings, russian_mentions=russian,
        blogger_mentions=bloggers, popularity_observations=popularity,
    )


def components(result):
    return {item.name: item for item in result.components}


def test_fragrantica_blends_independently_smoothed_rating_and_reactions(qa_config):
    signal = rating(mean_rating=4.0, rating_count=100, positive_count=80, negative_count=20)
    result = score_opinions(inputs(ratings=[signal]), configuration(qa_config))
    expected_rating = (100 * 4.0 + 10 * 3.0) / 110 / 5
    expected_reactions = (80 + 10 * 0.5) / 110
    assert components(result)["fragrantica"].value == pytest.approx((expected_rating + expected_reactions) / 2)
    assert result.total == pytest.approx((expected_rating + expected_reactions) / 2)
    assert components(result)["fragrantica"].evidence == (EvidenceRef(rating_observation_id=1),)


@pytest.mark.parametrize("changes,expected", [
    ({"mean_rating": 5.0, "rating_count": 10}, 0.8),
    ({"positive_count": 10, "negative_count": 0}, 0.75),
])
def test_fragrantica_redistributes_a_missing_branch_to_the_available_positive_share(qa_config, changes, expected):
    result = score_opinions(inputs(ratings=[rating(**changes)]), configuration(qa_config))
    assert result.total == pytest.approx(expected)


@pytest.mark.parametrize("changes", [
    {}, {"mean_rating": 5.0, "rating_count": 0}, {"mean_rating": 5.0},
    {"rating_count": 100}, {"positive_count": 0, "negative_count": 0},
    {"positive_count": 10}, {"negative_count": 10},
])
def test_prior_or_incomplete_counts_do_not_create_observed_fragrantica_data(qa_config, changes):
    result = score_opinions(inputs(ratings=[rating(**changes)]), configuration(qa_config))
    assert result.total is None
    assert "fragrantica" in result.missing
    assert result.components == ()


def test_zero_share_branch_cannot_rescue_a_missing_active_branch(qa_config):
    config = configuration(qa_config, policy={"fragrantica_rating_share": 1.0})
    result = score_opinions(inputs(ratings=[rating(positive_count=10, negative_count=0)]), config)
    assert result.total is None
    assert "fragrantica" in result.missing


def test_three_perfect_votes_rank_below_a_thousand_high_votes_after_smoothing(qa_config):
    config = configuration(qa_config, policy={"fragrantica_rating_share": 1.0})
    tiny = score_opinions(inputs(ratings=[rating(mean_rating=5.0, rating_count=3)]), config)
    robust = score_opinions(inputs(ratings=[rating(mean_rating=4.5, rating_count=1000)]), config)
    assert tiny.total < robust.total


def test_opinion_policy_and_bayesian_config_change_the_result(qa_config):
    data = inputs(ratings=[rating(mean_rating=5.0, rating_count=10, positive_count=0, negative_count=10)])
    rating_only = score_opinions(data, configuration(qa_config, policy={"fragrantica_rating_share": 1.0}))
    reactions_only = score_opinions(data, configuration(qa_config, policy={"fragrantica_rating_share": 0.0}))
    assert rating_only.total == pytest.approx(0.8)
    assert reactions_only.total == pytest.approx(0.25)
    stronger_prior = score_opinions(data, configuration(
        qa_config, policy={"fragrantica_rating_share": 1.0}, bayesian={"prior_count": 100},
    ))
    assert stronger_prior.total < rating_only.total


def test_fragrantica_uses_latest_snapshot_without_summing_cumulative_counts(qa_config):
    old = rating(1, mean_rating=1.0, rating_count=9999, observed_at=NOW - timedelta(days=1))
    latest = rating(2, mean_rating=5.0, rating_count=10)
    result = score_opinions(inputs(ratings=[latest, old]), configuration(qa_config))
    assert result.total == pytest.approx(0.8)
    assert components(result)["fragrantica"].evidence == (EvidenceRef(rating_observation_id=2),)


def test_russian_reviews_use_neutral_midpoint_and_configured_prior(qa_config):
    config = configuration(qa_config, weights={"russian_reviews": 1.0})
    data = inputs(russian=[mention(1), mention(2), mention(3, sentiment="neutral")])
    result = score_opinions(data, config)
    assert result.total == pytest.approx((2.5 + 10 * 0.5) / 13)
    assert {ref.mention_id for ref in components(result)["russian_reviews"].evidence} == {1, 2, 3}
    other_prior = configuration(qa_config, weights={"russian_reviews": 1.0}, policy={"russian_review_prior_mean": 0.0})
    assert score_opinions(data, other_prior).total == pytest.approx(2.5 / 13)


def test_llm_confidence_changes_neither_review_count_nor_bloggers_trust(qa_config):
    config = configuration(qa_config, weights={"russian_reviews": 0.5, "bloggers": 0.5})
    low = inputs(
        russian=[mention(1, confidence=0.01)],
        bloggers=[mention(2, source_key="youtube", blogger_id=7, applied_blogger_weight=0.2, confidence=0.01)],
    )
    high = inputs(
        russian=[replace(low.russian_mentions[0], confidence=1.0)],
        bloggers=[replace(low.blogger_mentions[0], confidence=1.0)],
    )
    assert score_opinions(low, config) == score_opinions(high, config)


def test_bloggers_average_per_author_then_apply_trust_without_post_count_bias(qa_config):
    config = configuration(qa_config, weights={"bloggers": 1.0})
    prolific = [mention(index, source_key="youtube", blogger_id=7, applied_blogger_weight=0.25) for index in range(1, 101)]
    trusted_negative = mention(101, source_key="youtube", blogger_id=8, sentiment="negative", applied_blogger_weight=0.75)
    result = score_opinions(inputs(bloggers=prolific + [trusted_negative]), config)
    assert result.total == pytest.approx(0.25)
    refs = components(result)["bloggers"].evidence
    assert len(refs) == 101
    assert {ref.applied_blogger_weight for ref in refs if ref.mention_id < 101} == {0.25}
    assert next(ref for ref in refs if ref.mention_id == 101).applied_blogger_weight == 0.75
    single_post = score_opinions(inputs(bloggers=[prolific[0], trusted_negative]), config)
    assert single_post.total == result.total


def test_mixed_sentiment_blogger_is_averaged_before_author_weights(qa_config):
    values = [
        mention(1, blogger_id=7, applied_blogger_weight=0.25),
        mention(2, blogger_id=7, sentiment="negative", applied_blogger_weight=0.25),
        mention(3, blogger_id=8, sentiment="negative", applied_blogger_weight=0.75),
    ]
    result = score_opinions(inputs(bloggers=values), configuration(qa_config, weights={"bloggers": 1.0}))
    assert result.total == pytest.approx(0.125)


def test_zero_trust_blogger_is_disabled_and_does_not_create_a_component(qa_config):
    result = score_opinions(
        inputs(bloggers=[mention(blogger_id=7, applied_blogger_weight=0.0)]),
        configuration(qa_config, weights={"bloggers": 1.0}),
    )
    assert result.total is None
    assert "bloggers" in result.missing


@pytest.mark.parametrize("gap,expected,direction", [(0.11, 1.0, None), (0.09, 0.0, "fragrantica_higher")])
def test_consensus_is_binary_only_between_fragrantica_and_russian_reviews(qa_config, gap, expected, direction):
    config = configuration(qa_config, weights={"consensus": 1.0}, thresholds={"consensus_max_gap": gap})
    data = inputs(ratings=[rating(mean_rating=3.0, rating_count=10)], russian=[mention(sentiment="neutral")])
    result = score_opinions(data, config)
    assert result.total == expected
    assert result.disagreement == direction
    assert {ref.rating_observation_id for ref in components(result)["consensus"].evidence if ref.rating_observation_id} == {1}
    assert {ref.mention_id for ref in components(result)["consensus"].evidence if ref.mention_id} == {1}


def test_consensus_cannot_substitute_bloggers_for_missing_russian_reviews(qa_config):
    config = configuration(qa_config, weights={"consensus": 1.0})
    result = score_opinions(inputs(
        ratings=[rating(mean_rating=5.0, rating_count=100)],
        bloggers=[mention(2, blogger_id=7, applied_blogger_weight=1.0)],
    ), config)
    assert result.total is None
    assert "consensus" in result.missing
    assert result.disagreement is None


def test_consensus_threshold_includes_exact_equality_and_reports_both_directions(qa_config):
    config = configuration(qa_config, weights={"consensus": 1.0}, thresholds={"consensus_max_gap": 0.0})
    equal = inputs(ratings=[rating(positive_count=1, negative_count=1)], russian=[mention(sentiment="neutral")])
    assert score_opinions(equal, config).total == 1.0
    higher_r = inputs(ratings=[rating(mean_rating=0.0, rating_count=1000)], russian=[mention()])
    result = score_opinions(higher_r, config)
    assert result.total == 0.0
    assert result.disagreement == "russian_reviews_higher"


def test_rank_is_neither_a_fragrantica_rating_nor_a_vote_count(qa_config):
    rank = rating(source_key="dataset", popularity_rank=1)
    result = score_opinions(inputs(popularity=[rank]), configuration(qa_config, weights={"hype": 1.0}))
    assert result.total == -1.0
    assert {"fragrantica", "novelty", "taste"} <= set(result.missing)


@pytest.mark.parametrize("count,novelty,hype", [(0, 1.0, 0.0), (50, 0.5, 0.0), (100, 0.0, 0.0), (1000, 0.0, 1.0)])
def test_novelty_and_hype_preserve_dataset_count_formulas(qa_config, count, novelty, hype):
    result = score_opinions(
        inputs(popularity=[rating(source_key="dataset", rating_count=count)]),
        configuration(qa_config, weights={"novelty": 0.5, "hype": 0.5}),
    )
    assert components(result)["novelty"].value == novelty
    assert components(result)["hype"].value == hype
    assert components(result)["hype"].effective_weight == -0.5
    assert result.total == pytest.approx((novelty - hype) / 2)


def test_current_per_source_popularity_preserves_independent_rank_and_count(qa_config):
    values = [
        rating(1, source_key="rank_source", popularity_rank=1, observed_at=NOW - timedelta(hours=1)),
        rating(2, source_key="count_source", rating_count=0),
        rating(3, source_key="count_source", rating_count=9999, observed_at=NOW - timedelta(days=1)),
    ]
    result = score_opinions(inputs(popularity=values), configuration(qa_config, weights={"novelty": 0.5, "hype": 0.5}))
    assert components(result)["novelty"].value == 1.0
    assert components(result)["hype"].value == 1.0
    assert components(result)["novelty"].evidence == (EvidenceRef(rating_observation_id=2),)
    assert {ref.rating_observation_id for ref in components(result)["hype"].evidence} == {1, 2}


def test_missing_components_redistribute_weights_but_observed_zero_still_participates(qa_config):
    config = configuration(qa_config, weights={"russian_reviews": 0.2, "taste": 0.8}, policy={"russian_review_prior_count": 0})
    result = score_opinions(inputs(russian=[mention(sentiment="negative")]), config)
    assert result.total == 0.0
    assert components(result)["russian_reviews"].effective_weight == 1.0
    assert "russian_reviews" not in result.missing
    assert "taste" in result.missing
    assert score_opinions(inputs(), config).total is None


def test_final_total_uses_effective_signed_weights_and_keeps_evidence_references(qa_config):
    config = configuration(qa_config, weights={"russian_reviews": 0.2, "hype": 0.3, "taste": 0.5})
    result = score_opinions(inputs(
        russian=[mention()], popularity=[rating(2, source_key="dataset", popularity_rank=1)],
    ), config)
    assert components(result)["russian_reviews"].effective_weight == pytest.approx(0.4)
    assert components(result)["hype"].effective_weight == pytest.approx(-0.6)
    assert result.total == pytest.approx((6 / 11) * 0.4 - 0.6)
    assert result.algorithm_version == "opinions-v1"


def test_identical_mention_id_is_deduplicated_and_latest_document_version_replaces_old(qa_config):
    old = mention(1, sentiment="negative", document_key="review-a", observed_at=NOW - timedelta(hours=1))
    new = mention(2, document_key="review-a")
    result = score_opinions(inputs(russian=[old, new, new]), configuration(qa_config, weights={"russian_reviews": 1.0}))
    assert result.total == pytest.approx(6 / 11)
    assert components(result)["russian_reviews"].evidence == (EvidenceRef(mention_id=2),)


@pytest.mark.parametrize("kind", ["conflicting_id", "conflicting_document", "overlapping_channels", "inconsistent_blogger_trust"])
def test_conflicting_evidence_is_rejected_instead_of_inflating_counts(qa_config, kind):
    if kind == "conflicting_id":
        data = inputs(russian=[mention(1), mention(1, sentiment="negative")])
    elif kind == "conflicting_document":
        data = inputs(russian=[mention(1, document_key="doc"), mention(2, document_key="doc", sentiment="negative")])
    elif kind == "overlapping_channels":
        data = inputs(russian=[mention(1)], bloggers=[mention(1, blogger_id=7, applied_blogger_weight=1.0)])
    else:
        data = inputs(bloggers=[mention(1, blogger_id=7, applied_blogger_weight=0.25), mention(2, blogger_id=7, applied_blogger_weight=0.75)])
    with pytest.raises((ValueError, TypeError)):
        score_opinions(data, configuration(qa_config))


@pytest.mark.parametrize("changes", [
    {"mean_rating": float("nan")}, {"mean_rating": 5.1}, {"mean_rating": -0.1},
    {"rating_count": -1}, {"rating_count": True}, {"rating_count": 1.2},
    {"positive_count": -1}, {"negative_count": -1}, {"popularity_rank": 0},
    {"observed_at": NOW.replace(tzinfo=None)}, {"id": 0},
])
def test_invalid_numeric_or_naive_rating_evidence_is_rejected(qa_config, changes):
    with pytest.raises((ValueError, TypeError)):
        score_opinions(inputs(ratings=[rating(**changes)]), configuration(qa_config))


@pytest.mark.parametrize("changes", [
    {"sentiment": "mixed"}, {"confidence": float("nan")}, {"confidence": 1.1},
    {"confidence": True}, {"observed_at": NOW.replace(tzinfo=None)},
    {"published_at": NOW.replace(tzinfo=None)}, {"applied_blogger_weight": -1.0, "blogger_id": 7},
    {"applied_blogger_weight": float("inf"), "blogger_id": 7},
])
def test_invalid_mention_evidence_is_rejected(qa_config, changes):
    with pytest.raises((ValueError, TypeError)):
        score_opinions(inputs(bloggers=[mention(**changes)]), configuration(qa_config))


@pytest.mark.parametrize("changes", [{}, {"blogger_id": 7}, {"applied_blogger_weight": 1.0}])
def test_blogger_component_requires_explicit_identity_and_trust(qa_config, changes):
    with pytest.raises((ValueError, TypeError)):
        score_opinions(inputs(bloggers=[mention(**changes)]), configuration(qa_config))


def test_full_opinion_scoring_requires_explicit_approved_opinion_policy(qa_config):
    config = configuration(qa_config)
    config.opinion_components = None
    with pytest.raises((ValueError, TypeError)):
        score_opinions(inputs(ratings=[rating(mean_rating=5.0, rating_count=10)]), config)


def test_nonzero_consensus_boundary_is_inclusive_despite_float_roundoff(qa_config):
    config = configuration(
        qa_config, weights={"consensus": 1.0}, policy={"russian_review_prior_count": 0},
        thresholds={"consensus_max_gap": 0.2},
    )
    data = inputs(
        ratings=[rating(mean_rating=3.0, rating_count=10)],
        russian=[mention(index, sentiment="positive" if index < 5 else "negative") for index in range(1, 6)],
    )
    result = score_opinions(data, config)
    assert result.total == 1.0
    assert result.disagreement is None


def test_reordered_inputs_and_repeated_scoring_are_deterministic_and_do_not_mutate_inputs(qa_config):
    config = configuration(qa_config, weights={name: 0.1 for name in NAMES})
    ratings = [rating(1, mean_rating=4.0, rating_count=100), rating(2, mean_rating=3.0, rating_count=20)]
    russian = [mention(3), mention(4, sentiment="negative")]
    bloggers = [mention(5, blogger_id=7, applied_blogger_weight=0.75), mention(6, blogger_id=8, applied_blogger_weight=0.25)]
    popularity = [rating(7, source_key="rank_source", popularity_rank=1), rating(8, source_key="count_source", rating_count=0)]
    data = inputs(ratings=ratings, russian=russian, bloggers=bloggers, popularity=popularity)
    snapshots = tuple(tuple(value) for value in (ratings, russian, bloggers, popularity))
    first = score_opinions(data, config)
    assert score_opinions(data, config) == first
    reversed_data = inputs(
        ratings=list(reversed(ratings)), russian=list(reversed(russian)),
        bloggers=list(reversed(bloggers)), popularity=list(reversed(popularity)),
    )
    assert score_opinions(reversed_data, config) == first
    assert tuple(tuple(value) for value in (ratings, russian, bloggers, popularity)) == snapshots
    assert all(isinstance(item.evidence, tuple) for item in first.components)


def test_latest_incomplete_snapshot_does_not_resurrect_old_fragrantica_signals(qa_config):
    data = inputs(ratings=[
        rating(1, mean_rating=5.0, rating_count=1000, observed_at=NOW - timedelta(days=1)),
        rating(2),
    ])
    result = score_opinions(data, configuration(qa_config))
    assert result.total is None
    assert "fragrantica" in result.missing


@pytest.mark.parametrize("count,expected", [(0, 1.0), (1, 0.0)])
def test_zero_novelty_cutoff_has_an_explicit_boundary(qa_config, count, expected):
    config = configuration(qa_config, weights={"novelty": 1.0}, thresholds={"novelty_max_rating_count": 0})
    result = score_opinions(inputs(popularity=[rating(rating_count=count)]), config)
    assert result.total == expected


def test_score_refs_can_be_persisted_and_traced_to_actual_rating_mentions_and_blogger_weight(
    qa_engine, qa_seed, qa_config,
):
    perfume_id = qa_seed(1)[0]
    config = configuration(qa_config, weights={name: 0.1 for name in NAMES})
    with qa_engine.begin() as connection:
        source_ids = {}
        for key, kind in (("fragrantica", "review"), ("otzovik", "review"), ("youtube", "blog")):
            source_ids[key] = connection.execute(insert(Source).values(
                key=key, kind=kind, name=f"Synthetic QA {key}", enabled=True,
                permission_status="approved", access_basis="Synthetic test only",
            )).inserted_primary_key[0]
        blogger_id = connection.execute(insert(Blogger).values(
            source_id=source_ids["youtube"], external_id="qa-blogger", name="QA Blogger", weight=0.75,
        )).inserted_primary_key[0]
        rating_id = connection.execute(insert(RatingObservation).values(
            perfume_id=perfume_id, source_id=source_ids["fragrantica"], source_kind="review",
            external_id="qa-rating", source_url="https://example.test/ratings/1", observed_at=NOW,
            mean_rating=4.0, rating_count=1000, positive_count=80, negative_count=20,
        )).inserted_primary_key[0]
        mention_ids = []
        for source, author, external, quote, sentiment in (
            ("otzovik", None, "qa-review", "QA Cedar нравится", "positive"),
            ("youtube", blogger_id, "qa-blog", "QA Cedar не нравится", "negative"),
        ):
            mention_ids.append(connection.execute(insert(Mention).values(
                perfume_id=perfume_id, source_id=source_ids[source], blogger_id=author,
                external_id=external, source_url=f"https://example.test/{external}",
                sentiment=sentiment, evidence_quote=quote, published_at=None, confidence=0.9,
            )).inserted_primary_key[0])
        signal = rating(rating_id, mean_rating=4.0, rating_count=1000, positive_count=80, negative_count=20)
        result = score_opinions(inputs(
            ratings=[signal], russian=[mention(mention_ids[0])],
            bloggers=[mention(mention_ids[1], source_key="youtube", blogger_id=blogger_id,
                              sentiment="negative", applied_blogger_weight=0.75)],
            popularity=[signal],
        ), config)
        snapshot_id = connection.execute(insert(ScoreSnapshot).values(
            perfume_id=perfume_id, computed_at=NOW, algorithm_version=result.algorithm_version,
            scoring_config=config.model_dump(mode="json"), total=result.total,
        )).inserted_primary_key[0]
        expected = set()
        for component in result.components:
            component_id = connection.execute(insert(ScoreComponent).values(
                snapshot_id=snapshot_id, perfume_id=perfume_id, name=component.name,
                value=component.value, effective_weight=component.effective_weight,
            )).inserted_primary_key[0]
            for ref in component.evidence:
                connection.execute(insert(ScoreEvidence).values(
                    component_id=component_id, perfume_id=perfume_id,
                    rating_observation_id=ref.rating_observation_id, mention_id=ref.mention_id,
                    applied_blogger_weight=ref.applied_blogger_weight,
                ))
                expected.add((component.name, ref.rating_observation_id, ref.mention_id, ref.applied_blogger_weight))
        stored = connection.execute(text("""
            SELECT c.name,e.rating_observation_id,e.mention_id,e.applied_blogger_weight
            FROM score_evidence e JOIN score_components c ON c.id=e.component_id
            WHERE c.snapshot_id=:snapshot
        """), {"snapshot": snapshot_id}).all()
        assert {tuple(row) for row in stored} == expected
        blogger_trace = connection.execute(text("""
            SELECT s.key,m.external_id,m.source_url,m.evidence_quote,b.external_id,
                   b.weight,e.applied_blogger_weight
            FROM score_evidence e JOIN score_components c ON c.id=e.component_id
            JOIN mentions m ON m.id=e.mention_id JOIN sources s ON s.id=m.source_id
            JOIN bloggers b ON b.id=m.blogger_id
            WHERE c.snapshot_id=:snapshot AND c.name='bloggers'
        """), {"snapshot": snapshot_id}).one()
        assert tuple(blogger_trace) == (
            "youtube", "qa-blog", "https://example.test/qa-blog", "QA Cedar не нравится", "qa-blogger", 0.75, 0.75,
        )
        assert connection.execute(select(ScoreSnapshot.scoring_config).where(ScoreSnapshot.id == snapshot_id)).scalar_one() == json.loads(config.model_dump_json())
