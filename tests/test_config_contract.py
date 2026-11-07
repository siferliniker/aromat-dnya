from datetime import time
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config import load_config
from storage.contracts import ScoreComponents


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
COMPONENTS = {
    "fragrantica", "russian_reviews", "bloggers", "consensus", "novelty", "hype", "taste"
}


def test_config_sets_default_delivery_recommendation_and_provider():
    config = load_config(CONFIG_PATH)
    assert config.delivery.timezone == "Asia/Omsk"
    assert config.delivery.send_at == time(10)
    assert config.delivery.language == "ru"
    assert config.recommendation.exploration_ratio == 0.3
    assert 1 - config.recommendation.exploration_ratio == pytest.approx(0.7)
    assert config.llm.provider == "openai"


def test_mvp_enables_only_the_approved_offline_catalog_and_no_live_llm():
    config = load_config(CONFIG_PATH)
    assert config.sources["dataset"].enabled
    assert not any(source.enabled for key, source in config.sources.items() if key != "dataset")
    assert not config.llm.enabled


def test_config_loads_all_formula_parameters(tmp_path):
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    weights = raw_config["scoring"]["weights"]
    assert COMPONENTS <= weights.keys()
    weights["fragrantica"] = weights["fragrantica"] / 2
    custom = tmp_path / "custom.yaml"
    custom.write_text(yaml.safe_dump(raw_config), encoding="utf-8")

    scoring = load_config(custom).scoring
    assert scoring.weights.model_dump() == weights
    assert all(weight >= 0 for weight in weights.values())
    assert scoring.hype_sign == -1
    assert scoring.missing_data == "redistribute_available"
    assert (scoring.normalization.component_min, scoring.normalization.component_max) == (0, 1)
    assert 0 < scoring.bayesian.rating_prior_mean <= 5
    assert 0 <= scoring.bayesian.reaction_prior_positive <= 1
    assert scoring.bayesian.prior_count > 0
    assert 0 <= scoring.thresholds.novelty_max_rating_count < scoring.thresholds.hype_min_rating_count
    assert scoring.thresholds.hype_max_popularity_rank > 0
    assert 0 <= scoring.thresholds.consensus_max_gap <= 1


def test_score_components_preserve_missing_zero_and_one():
    components = ScoreComponents(fragrantica=0, taste=1)
    restored = ScoreComponents.model_validate_json(components.model_dump_json())
    assert restored.model_dump() == dict.fromkeys(COMPONENTS) | {"fragrantica": 0, "taste": 1}


@pytest.mark.parametrize("component", sorted(COMPONENTS))
@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_each_score_component_rejects_out_of_range_values(component, value):
    with pytest.raises(ValidationError):
        ScoreComponents.model_validate({component: value})
