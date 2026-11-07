import json

import pytest
from pydantic import ValidationError

from llm.contracts import CardSummary, ReviewExtraction, build_cache_key


def valid_mention():
    return {
        "perfume_name": "Тестовый аромат",
        "perfume_house": None,
        "sentiment": "positive",
        "evidence_quote": "мне понравился древесный аккорд",
        "confidence": None,
    }


def test_extraction_accepts_json_with_evidence_and_explicit_unknown_values():
    payload = {"mentions": [valid_mention()]}
    result = ReviewExtraction.model_validate_json(json.dumps(payload))
    assert result.model_dump() == payload
    assert ReviewExtraction.model_validate_json('{"mentions": []}').mentions == []


@pytest.mark.parametrize(
    "changes",
    [
        {"sentiment": "very_positive"},
        {"confidence": 1.1},
        {"confidence": "0.5"},
        {"invented_note": "мускус"},
    ],
)
def test_extraction_rejects_invalid_values_coercion_and_extra_fields(changes):
    payload = {"mentions": [valid_mention() | changes]}
    with pytest.raises(ValidationError):
        ReviewExtraction.model_validate_json(json.dumps(payload))


def test_extraction_rejects_missing_nullable_keys_and_json_wrapped_in_prose():
    mention = valid_mention()
    del mention["perfume_house"]
    with pytest.raises(ValidationError):
        ReviewExtraction.model_validate_json(json.dumps({"mentions": [mention]}))
    with pytest.raises(ValidationError):
        ReviewExtraction.model_validate_json('```json\n{"mentions": []}\n```')


def test_summary_contract_keeps_mention_ids_and_rejects_extra_fields():
    data = {"text": "Хвалят древесный аккорд.", "evidence_mention_ids": [7]}
    assert CardSummary.model_validate_json(json.dumps(data)).model_dump() == data
    for changes in ({"evidence_mention_ids": ["7"]}, {"rating": 4.8}):
        with pytest.raises(ValidationError):
            CardSummary.model_validate_json(json.dumps(data | changes))


def test_summary_rejects_generated_text_without_supporting_mentions():
    payload = {"text": "Хвалят древесный аккорд.", "evidence_mention_ids": []}
    with pytest.raises(ValidationError):
        CardSummary.model_validate_json(json.dumps(payload))


def test_summary_rejects_whitespace_in_place_of_summary_text():
    payload = {"text": " \n\t", "evidence_mention_ids": [7]}
    with pytest.raises(ValidationError):
        CardSummary.model_validate_json(json.dumps(payload))


def test_cache_key_separates_tasks_versions_models_and_input_text():
    values = {
        "task": "extraction",
        "model": "fixture-model",
        "prompt_version": "1",
        "schema_version": "1",
        "input_text": "Синтетическое упоминание",
    }
    original = build_cache_key(**values)
    assert build_cache_key(**values) == original
    assert len(original) == 64
    assert set(original) <= set("0123456789abcdef")
    changes = {
        "task": "summary",
        "model": "other-model",
        "prompt_version": "2",
        "schema_version": "2",
        "input_text": "Синтетическое упоминание.",
    }
    for name, value in changes.items():
        assert build_cache_key(**(values | {name: value})) != original
