"""Synthetic self-tests of the evaluator, never the project's human golden set."""

from copy import deepcopy
import hashlib
import json

import pytest
from pydantic import ValidationError

from golden_eval import GoldenDataset, PredictionSet, evaluate, main


@pytest.fixture
def synthetic_golden():
    cases = []
    for index in range(40):
        text = f"Синтетический QA пример {index}: нравится QA аромат."
        cases.append({
            "case_id": f"fixture-{index}", "text": text,
            "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "rights": {
                "source_key": "qa_fixture", "source_url": "https://example.test/fixtures",
                "approval_id": "qa-attestation-fixture-only", "basis": "Synthetic validator self-test",
                "source_access_allowed": True, "llm_processing_allowed": True,
                "quotation_allowed": True, "retention_allowed": True,
            },
            "labels": [{"perfume_id": 1, "sentiment": "positive", "evidence_quote": "нравится QA аромат"}],
        })
    return {
        "schema_version": "1", "dataset_id": "synthetic-evaluator-self-test-only",
        "approval": {
            "status": "approved", "label_method": "human", "approved_by": "QA attestation validation fixture",
            "approved_at": "2026-11-07T00:00:00Z",
        },
        "cases": cases,
    }


@pytest.fixture
def synthetic_predictions(synthetic_golden):
    return {
        "schema_version": "1", "dataset_id": synthetic_golden["dataset_id"],
        "model": "offline-fixture-model", "prompt_version": "qa-1", "extraction_schema_version": "1",
        "cases": [{
            "case_id": case["case_id"], "input_sha256": case["input_sha256"],
            "mentions": [{"perfume_id": 1, "sentiment": "positive"}],
        } for case in synthetic_golden["cases"]],
    }


def parsed_report(golden, predictions):
    return evaluate(GoldenDataset.model_validate_json(json.dumps(golden)),
                    PredictionSet.model_validate_json(json.dumps(predictions)))


def test_perfect_synthetic_self_test_reports_all_metrics(synthetic_golden, synthetic_predictions):
    report = parsed_report(synthetic_golden, synthetic_predictions)
    assert report.accepted
    assert report.expected_mentions == report.predicted_mentions == report.matched_names == 40
    assert report.name_precision == report.name_recall == report.name_micro_f1 == 1
    assert report.sentiment_accuracy == report.strict_document_accuracy == 1


@pytest.mark.parametrize("count", [0, 1, 29, 51])
def test_acceptance_rejects_missing_or_out_of_range_case_count(synthetic_golden, count):
    synthetic_golden["cases"] = synthetic_golden["cases"][:count]
    if count == 51:
        synthetic_golden["cases"] *= 2
    with pytest.raises(ValidationError):
        GoldenDataset.model_validate_json(json.dumps(synthetic_golden))


@pytest.mark.parametrize("field,value", [("status", "pending"), ("label_method", "generated"), ("approved_by", " ")])
def test_no_unapproved_or_generated_attestation_passes(synthetic_golden, field, value):
    synthetic_golden["approval"][field] = value
    with pytest.raises(ValidationError):
        GoldenDataset.model_validate_json(json.dumps(synthetic_golden))


@pytest.mark.parametrize("field", ["source_access_allowed", "llm_processing_allowed", "quotation_allowed", "retention_allowed"])
def test_every_text_requires_each_rights_attestation(synthetic_golden, field):
    synthetic_golden["cases"][0]["rights"][field] = False
    with pytest.raises(ValidationError):
        GoldenDataset.model_validate_json(json.dumps(synthetic_golden))


def test_empty_label_set_cannot_turn_true_negatives_into_an_acceptance_pass(synthetic_golden):
    for case in synthetic_golden["cases"]:
        case["labels"] = []
    with pytest.raises(ValidationError):
        GoldenDataset.model_validate_json(json.dumps(synthetic_golden))


@pytest.mark.parametrize("field", ["case_id", "text"])
def test_duplicate_inputs_or_ids_cannot_inflate_golden_size(synthetic_golden, field):
    synthetic_golden["cases"][1][field] = synthetic_golden["cases"][0][field]
    if field == "text":
        synthetic_golden["cases"][1]["input_sha256"] = synthetic_golden["cases"][0]["input_sha256"]
    with pytest.raises(ValidationError):
        GoldenDataset.model_validate_json(json.dumps(synthetic_golden))


@pytest.mark.parametrize("corruption", ["digest", "quote", "duplicate_label"])
def test_etalon_keeps_input_and_exact_evidence_integrity(synthetic_golden, corruption):
    case = synthetic_golden["cases"][0]
    if corruption == "digest":
        case["input_sha256"] = "a" * 64
    elif corruption == "quote":
        case["labels"][0]["evidence_quote"] = "invented quotation"
    else:
        case["labels"].append(deepcopy(case["labels"][0]))
    with pytest.raises(ValidationError):
        GoldenDataset.model_validate_json(json.dumps(synthetic_golden))


@pytest.mark.parametrize("corruption", ["missing_case", "extra_case", "duplicate_case", "digest", "dataset"])
def test_predictions_cannot_omit_or_swap_evaluated_inputs(synthetic_golden, synthetic_predictions, corruption):
    if corruption == "missing_case":
        synthetic_predictions["cases"].pop()
    elif corruption == "extra_case":
        synthetic_predictions["cases"].append(synthetic_predictions["cases"][0] | {"case_id": "unexpected"})
    elif corruption == "duplicate_case":
        synthetic_predictions["cases"].append(deepcopy(synthetic_predictions["cases"][0]))
    elif corruption == "digest":
        synthetic_predictions["cases"][0]["input_sha256"] = "b" * 64
    else:
        synthetic_predictions["dataset_id"] = "different-dataset"
    with pytest.raises((ValidationError, ValueError)):
        parsed_report(synthetic_golden, synthetic_predictions)


def test_extra_hallucinated_names_reduce_precision_and_f1(synthetic_golden, synthetic_predictions):
    for case in synthetic_predictions["cases"]:
        case["mentions"].append({"perfume_id": 2, "sentiment": "positive"})
    report = parsed_report(synthetic_golden, synthetic_predictions)
    assert report.name_recall == report.sentiment_accuracy == 1
    assert report.name_precision == 0.5
    assert report.name_micro_f1 == pytest.approx(2 / 3)
    assert report.strict_document_accuracy == 0
    assert not report.accepted


def test_missing_names_count_wrong_in_sentiment_denominator(synthetic_golden, synthetic_predictions):
    for case in synthetic_predictions["cases"][:8]:
        case["mentions"] = []
    report = parsed_report(synthetic_golden, synthetic_predictions)
    assert report.name_precision == 1
    assert report.name_recall == report.sentiment_accuracy == 0.8
    assert report.name_micro_f1 == pytest.approx(64 / 72)
    assert not report.accepted


def test_empty_predictions_are_a_failure_with_defined_zero_metrics(synthetic_golden, synthetic_predictions):
    for case in synthetic_predictions["cases"]:
        case["mentions"] = []
    report = parsed_report(synthetic_golden, synthetic_predictions)
    assert report.name_precision == report.name_recall == report.name_micro_f1 == 0
    assert report.sentiment_accuracy == 0
    assert not report.accepted


@pytest.mark.parametrize("wrong_names,wrong_sentiments,accepted", [(4, 2, True), (5, 0, False), (4, 3, False)])
def test_both_thresholds_are_required_and_boundaries_are_inclusive(
    synthetic_golden, synthetic_predictions, wrong_names, wrong_sentiments, accepted
):
    for case in synthetic_predictions["cases"][:wrong_names]:
        case["mentions"][0]["perfume_id"] = 2
    for case in synthetic_predictions["cases"][wrong_names:wrong_names + wrong_sentiments]:
        case["mentions"][0]["sentiment"] = "negative"
    report = parsed_report(synthetic_golden, synthetic_predictions)
    assert report.accepted is accepted
    if accepted:
        assert report.name_micro_f1 == 0.9
        assert report.sentiment_accuracy == 0.85


@pytest.mark.parametrize("mode,exit_code", [("valid", 0), ("low_quality", 1), ("invalid", 2)])
def test_cli_exit_codes_are_machine_readable_without_printing_texts(
    synthetic_golden, synthetic_predictions, tmp_path, capsys, mode, exit_code
):
    if mode == "low_quality":
        for case in synthetic_predictions["cases"]:
            case["mentions"] = []
    if mode == "invalid":
        synthetic_golden["approval"]["status"] = "pending"
    golden_path, predictions_path = tmp_path / "golden.json", tmp_path / "predictions.json"
    golden_path.write_text(json.dumps(synthetic_golden, ensure_ascii=False), encoding="utf-8")
    predictions_path.write_text(json.dumps(synthetic_predictions, ensure_ascii=False), encoding="utf-8")
    assert main(["--golden", str(golden_path), "--predictions", str(predictions_path)]) == exit_code
    output = capsys.readouterr().out
    assert "Синтетический QA пример" not in output
    assert "нравится QA аромат" not in output
    assert json.loads(output)["accepted"] is (exit_code == 0)
