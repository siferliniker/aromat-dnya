import pytest

from scoring.dataset import score_dataset
from storage.models import RatingObservation


def observation(**values):
    return RatingObservation(id=42, **values)


def by_name(result):
    return {component.name: component for component in result.components}


def test_dataset_rank_does_not_invent_fragrantica_votes_or_novelty(qa_config):
    result = score_dataset(observation(popularity_rank=1), qa_config.scoring)
    components = by_name(result)
    assert set(components) == {"hype"}
    assert {"fragrantica", "russian_reviews", "bloggers", "consensus", "novelty", "taste"} <= set(result.missing)
    assert components["hype"].effective_weight == -1
    assert result.total == pytest.approx(-components["hype"].value)
    assert components["hype"].rating_observation_ids == (42,)


@pytest.mark.parametrize("rank", [1, 2, 99, 100, 101, 1479, 1000000])
def test_rank_penalty_is_normalized(qa_config, rank):
    result = score_dataset(observation(popularity_rank=rank), qa_config.scoring)
    assert 0 <= by_name(result)["hype"].value <= 1
    assert -1 <= result.total <= 0


def test_more_popular_rank_cannot_receive_a_smaller_hype_penalty(qa_config):
    values = [by_name(score_dataset(observation(popularity_rank=rank), qa_config.scoring))["hype"].value
              for rank in [1, 20, 100, 300, 1479]]
    assert values == sorted(values, reverse=True)
    assert values[0] > values[-1]


def test_observed_zero_is_available_while_missing_observation_is_not(qa_config):
    absent = score_dataset(None, qa_config.scoring)
    assert absent.total is None
    assert not absent.components
    assert "novelty" in absent.missing
    zero = score_dataset(observation(rating_count=0), qa_config.scoring)
    assert "novelty" in by_name(zero)
    assert "novelty" not in zero.missing
    assert zero.total is not None


def test_available_weights_follow_config_and_redistribute_with_signed_hype(qa_config):
    scoring = qa_config.scoring.model_copy(deep=True)
    scoring.weights.novelty = 0.3
    scoring.weights.hype = 0.1
    result = score_dataset(observation(rating_count=0, popularity_rank=1), scoring)
    components = by_name(result)
    assert set(components) == {"novelty", "hype"}
    assert components["novelty"].effective_weight == pytest.approx(0.75)
    assert components["hype"].effective_weight == pytest.approx(-0.25)
    assert sum(abs(c.effective_weight) for c in result.components) == pytest.approx(1)
    assert result.total == pytest.approx(sum(c.value * c.effective_weight for c in result.components))


def test_available_components_with_zero_config_weights_do_not_create_a_score(qa_config):
    scoring = qa_config.scoring.model_copy(deep=True)
    scoring.weights.novelty = 0.0
    scoring.weights.hype = 0.0
    result = score_dataset(observation(rating_count=0, popularity_rank=1), scoring)
    assert result.total is None
    assert not result.components
