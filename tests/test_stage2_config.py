"""Independent, offline acceptance tests for the approved stage-two config RFC."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import config as configuration


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
OPINION_POLICY = {
    "fragrantica_rating_share": 0.5,
    "russian_review_prior_mean": 0.5,
    "russian_review_prior_count": 10,
}
FETCH = {
    "request_timeout_seconds": 5.0,
    "source_deadline_seconds": 15.0,
    "max_attempts": 3,
    "retry_backoff_seconds": [0.5, 1.0],
    "min_request_interval_seconds": 0.2,
    "max_items_per_source": 20,
}
LLM = {
    "request_timeout_seconds": 10.0,
    "max_attempts": 2,
    "retry_backoff_seconds": [1.0],
    "max_input_chars": 8000,
    "max_output_tokens": 1000,
    "max_requests_per_run": 10,
    "max_concurrent_requests": 2,
}


@pytest.fixture
def legacy_config_data():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    data.pop("stage2", None)
    data["scoring"].pop("opinion_components", None)
    return data


@pytest.fixture
def enabled_config_data(legacy_config_data):
    data = deepcopy(legacy_config_data)
    data["scoring"]["opinion_components"] = deepcopy(OPINION_POLICY)
    data["llm"].update(enabled=True, model="qa-offline-model")
    data["sources"]["youtube"]["enabled"] = True
    data["sources"]["otzovik"]["enabled"] = True
    data["stage2"] = {
        "enabled": True,
        "approvals_path": "test-fixtures/approvals.json",
        "fetch": deepcopy(FETCH),
        "llm": deepcopy(LLM),
        "freshness_seconds": {"youtube": 3600, "otzovik": 7200},
        "claims_path": "test-fixtures/claims",
    }
    return data


def test_legacy_config_preserves_absent_stage2_and_absent_opinion_policy(legacy_config_data):
    result = configuration.AppConfig.model_validate(legacy_config_data)
    assert result.stage2 is None
    assert result.scoring.opinion_components is None
    assert not result.llm.enabled


def test_explicitly_disabled_stage2_needs_no_live_credentials_or_runtime_fields(legacy_config_data):
    legacy_config_data["stage2"] = {"enabled": False}
    result = configuration.AppConfig.model_validate(legacy_config_data)
    assert result.stage2.enabled is False
    for field in ("approvals_path", "fetch", "llm", "freshness_seconds", "claims_path"):
        assert getattr(result.stage2, field) is None


def test_checked_in_config_has_explicit_opinion_policy_and_disabled_stage2():
    result = configuration.load_config(CONFIG_PATH)
    assert result.scoring.opinion_components.model_dump() == OPINION_POLICY
    assert result.stage2.enabled is False
    assert result.llm.enabled is False
    assert result.llm.model is None


@pytest.mark.parametrize("missing", list(OPINION_POLICY))
def test_opinion_policy_never_fills_a_missing_required_parameter(missing):
    values = deepcopy(OPINION_POLICY)
    del values[missing]
    with pytest.raises(ValidationError):
        configuration.OpinionComponentsConfig.model_validate(values)


@pytest.mark.parametrize("field", ["fragrantica_rating_share", "russian_review_prior_mean"])
@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf"), True, "0.5"])
def test_opinion_ratios_are_finite_strict_normalized_numbers(field, value):
    with pytest.raises(ValidationError):
        configuration.OpinionComponentsConfig.model_validate(OPINION_POLICY | {field: value})


@pytest.mark.parametrize("field", ["fragrantica_rating_share", "russian_review_prior_mean"])
@pytest.mark.parametrize("value", [0.0, 1.0])
def test_opinion_ratios_accept_both_normalized_boundaries(field, value):
    result = configuration.OpinionComponentsConfig.model_validate(OPINION_POLICY | {field: value})
    assert getattr(result, field) == value


@pytest.mark.parametrize("value", [-1, True, 1.5, "10"])
def test_review_prior_count_is_a_strict_nonnegative_integer(value):
    with pytest.raises(ValidationError):
        configuration.OpinionComponentsConfig.model_validate(
            OPINION_POLICY | {"russian_review_prior_count": value}
        )


def test_zero_review_prior_count_is_explicitly_allowed():
    result = configuration.OpinionComponentsConfig.model_validate(
        OPINION_POLICY | {"russian_review_prior_count": 0}
    )
    assert result.russian_review_prior_count == 0


def test_complete_enabled_stage2_roundtrips_with_all_runtime_limits(enabled_config_data):
    result = configuration.AppConfig.model_validate(enabled_config_data)
    restored = configuration.AppConfig.model_validate_json(result.model_dump_json())
    assert restored.stage2.model_dump() == enabled_config_data["stage2"]


@pytest.mark.parametrize("missing", ["approvals_path", "fetch", "llm", "freshness_seconds", "claims_path"])
def test_enabled_stage2_rejects_each_missing_runtime_dependency(enabled_config_data, missing):
    del enabled_config_data["stage2"][missing]
    with pytest.raises(ValidationError):
        configuration.AppConfig.model_validate(enabled_config_data)


@pytest.mark.parametrize("missing", ["enabled", "model", "opinion_components"])
def test_enabled_stage2_requires_global_llm_and_explicit_score_policy(enabled_config_data, missing):
    if missing == "opinion_components":
        del enabled_config_data["scoring"][missing]
    elif missing == "model":
        enabled_config_data["llm"]["model"] = None
    else:
        enabled_config_data["llm"]["enabled"] = False
    with pytest.raises(ValidationError):
        configuration.AppConfig.model_validate(enabled_config_data)


@pytest.mark.parametrize("source_key", ["youtube", "otzovik"])
def test_each_enabled_opinion_source_needs_its_own_freshness_policy(enabled_config_data, source_key):
    del enabled_config_data["stage2"]["freshness_seconds"][source_key]
    with pytest.raises(ValidationError):
        configuration.AppConfig.model_validate(enabled_config_data)


def test_dataset_and_enabled_shop_or_manual_do_not_need_opinion_freshness(enabled_config_data):
    enabled_config_data["sources"]["randewoo"]["enabled"] = True
    enabled_config_data["sources"]["manual"]["enabled"] = True
    result = configuration.AppConfig.model_validate(enabled_config_data)
    assert set(result.stage2.freshness_seconds) == {"youtube", "otzovik"}


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3600"])
def test_freshness_is_a_strict_positive_integer(enabled_config_data, value):
    enabled_config_data["stage2"]["freshness_seconds"]["youtube"] = value
    with pytest.raises(ValidationError):
        configuration.AppConfig.model_validate(enabled_config_data)


@pytest.mark.parametrize("field", ["approvals_path", "claims_path"])
def test_approval_and_claim_paths_reject_blank_values(enabled_config_data, field):
    enabled_config_data["stage2"][field] = " \n\t"
    with pytest.raises(ValidationError):
        configuration.AppConfig.model_validate(enabled_config_data)


@pytest.mark.parametrize("value", ["false", 0, 1])
def test_stage2_enablement_is_strict_boolean(legacy_config_data, value):
    legacy_config_data["stage2"] = {"enabled": value}
    with pytest.raises(ValidationError):
        configuration.AppConfig.model_validate(legacy_config_data)


@pytest.mark.parametrize("model_name,values", [("FetchRuntimeConfig", FETCH), ("LLMRuntimeConfig", LLM)])
def test_runtime_limits_have_no_silent_defaults(model_name, values):
    model = getattr(configuration, model_name)
    for missing in values:
        incomplete = deepcopy(values)
        del incomplete[missing]
        with pytest.raises(ValidationError):
            model.model_validate(incomplete)


@pytest.mark.parametrize("model_name,values", [("FetchRuntimeConfig", FETCH), ("LLMRuntimeConfig", LLM)])
@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), True, "5"])
def test_request_timeouts_are_positive_finite_and_strict(model_name, values, value):
    with pytest.raises(ValidationError):
        getattr(configuration, model_name).model_validate(values | {"request_timeout_seconds": value})


@pytest.mark.parametrize("model_name,values", [("FetchRuntimeConfig", FETCH), ("LLMRuntimeConfig", LLM)])
@pytest.mark.parametrize("backoff", [[], [0.5, 1.0, 2.0], [-1.0, 1.0], [True, 1.0], [float("nan"), 1.0]])
def test_retry_schedule_matches_attempt_budget_and_contains_valid_delays(model_name, values, backoff):
    with pytest.raises(ValidationError):
        getattr(configuration, model_name).model_validate(
            values | {"max_attempts": 3, "retry_backoff_seconds": backoff}
        )


@pytest.mark.parametrize("model_name,values", [("FetchRuntimeConfig", FETCH), ("LLMRuntimeConfig", LLM)])
def test_one_attempt_has_an_empty_retry_schedule(model_name, values):
    result = getattr(configuration, model_name).model_validate(
        values | {"max_attempts": 1, "retry_backoff_seconds": []}
    )
    assert result.max_attempts == 1
    assert result.retry_backoff_seconds == []


def test_source_deadline_cannot_expire_before_one_request_timeout():
    with pytest.raises(ValidationError):
        configuration.FetchRuntimeConfig.model_validate(FETCH | {"source_deadline_seconds": 4.0})
    result = configuration.FetchRuntimeConfig.model_validate(FETCH | {"source_deadline_seconds": 5.0})
    assert result.source_deadline_seconds == result.request_timeout_seconds


@pytest.mark.parametrize(
    "model_name,values",
    [("OpinionComponentsConfig", OPINION_POLICY), ("FetchRuntimeConfig", FETCH), ("LLMRuntimeConfig", LLM)],
)
def test_new_nested_models_reject_unrecognized_settings(model_name, values):
    with pytest.raises(ValidationError):
        getattr(configuration, model_name).model_validate(values | {"unexpected": True})


def test_disabled_stage2_still_rejects_unrecognized_settings(legacy_config_data):
    legacy_config_data["stage2"] = {"enabled": False, "unexpected": True}
    with pytest.raises(ValidationError):
        configuration.AppConfig.model_validate(legacy_config_data)


def test_stage2_never_defaults_the_enablement_decision(legacy_config_data):
    legacy_config_data["stage2"] = {}
    with pytest.raises(ValidationError):
        configuration.AppConfig.model_validate(legacy_config_data)


@pytest.mark.parametrize(
    "model_name,values,fields",
    [
        ("FetchRuntimeConfig", FETCH, ["max_attempts", "max_items_per_source"]),
        ("LLMRuntimeConfig", LLM, ["max_attempts", "max_input_chars", "max_output_tokens", "max_requests_per_run", "max_concurrent_requests"]),
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "4"])
def test_every_request_and_item_budget_is_a_strict_positive_integer(model_name, values, fields, value):
    for field in fields:
        with pytest.raises(ValidationError):
            getattr(configuration, model_name).model_validate(values | {field: value})
