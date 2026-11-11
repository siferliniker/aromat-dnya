"""Offline golden-set validation and scoring; this module supplies no golden data.

The approval fields are an auditable attestation, not proof that a person reviewed
the labels. The project gate still requires independent human review of the set.
Run: python tests/golden_eval.py --golden approved.json --predictions run.json
"""

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, ValidationError,
    field_validator, model_validator,
)


Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveId = Annotated[int, Field(gt=0)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Sentiment = Literal["positive", "neutral", "negative"]


class GoldenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False,
        hide_input_in_errors=True, revalidate_instances="always",
    )


class HumanApproval(GoldenModel):
    status: Literal["approved"]
    label_method: Literal["human"]
    approved_by: Nonempty
    approved_at: AwareDatetime


class TextRights(GoldenModel):
    source_key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    source_url: AnyHttpUrl
    approval_id: Nonempty
    basis: Nonempty
    source_access_allowed: Literal[True]
    llm_processing_allowed: Literal[True]
    quotation_allowed: Literal[True]
    retention_allowed: Literal[True]

    @field_validator("source_url")
    @classmethod
    def exclude_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("source URL must not contain credentials")
        return value


class Label(GoldenModel):
    perfume_id: PositiveId
    sentiment: Sentiment


class ExpectedMention(Label):
    evidence_quote: Annotated[str, Field(min_length=1)]


class GoldenCase(GoldenModel):
    case_id: Nonempty
    text: Annotated[str, Field(min_length=1)]
    input_sha256: Digest
    rights: TextRights
    labels: list[ExpectedMention]

    @model_validator(mode="after")
    def verify_text_and_labels(self) -> Self:
        if self.text != self.text.strip():
            raise ValueError("text must equal the accepted ReviewTextRecord text")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.input_sha256:
            raise ValueError("input digest does not match text")
        if len({label.perfume_id for label in self.labels}) != len(self.labels):
            raise ValueError("one canonical perfume label per case is required")
        if any(label.evidence_quote not in self.text for label in self.labels):
            raise ValueError("expected evidence must be an exact input substring")
        return self


class GoldenDataset(GoldenModel):
    schema_version: Literal["1"]
    dataset_id: Nonempty
    approval: HumanApproval
    cases: Annotated[list[GoldenCase], Field(min_length=30, max_length=50)]

    @model_validator(mode="after")
    def reject_duplicate_and_empty_etalon(self) -> Self:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate case identifier")
        if len({case.input_sha256 for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate input text")
        if not any(case.labels for case in self.cases):
            raise ValueError("the set must contain expected perfume mentions")
        return self


class PredictedCase(GoldenModel):
    case_id: Nonempty
    input_sha256: Digest
    mentions: list[Label]

    @field_validator("mentions")
    @classmethod
    def reject_duplicate_perfumes(cls, value: list[Label]) -> list[Label]:
        if len({label.perfume_id for label in value}) != len(value):
            raise ValueError("duplicate predicted canonical perfume")
        return value


class PredictionSet(GoldenModel):
    schema_version: Literal["1"]
    dataset_id: Nonempty
    model: Nonempty
    prompt_version: Nonempty
    extraction_schema_version: Nonempty
    cases: Annotated[list[PredictedCase], Field(min_length=1)]

    @field_validator("cases")
    @classmethod
    def reject_duplicate_cases(cls, value: list[PredictedCase]) -> list[PredictedCase]:
        if len({case.case_id for case in value}) != len(value):
            raise ValueError("duplicate prediction case")
        return value


class EvaluationReport(GoldenModel):
    dataset_id: str
    case_count: int
    expected_mentions: int
    predicted_mentions: int
    matched_names: int
    correct_sentiments: int
    name_precision: float
    name_recall: float
    name_micro_f1: float
    sentiment_accuracy: float
    strict_document_accuracy: float
    accepted: bool


def evaluate(golden: GoldenDataset, predictions: PredictionSet) -> EvaluationReport:
    golden = GoldenDataset.model_validate(golden)
    predictions = PredictionSet.model_validate(predictions)
    if predictions.dataset_id != golden.dataset_id:
        raise ValueError("prediction dataset differs from the approved dataset")
    predicted_cases = {case.case_id: case for case in predictions.cases}
    if set(predicted_cases) != {case.case_id for case in golden.cases}:
        raise ValueError("predictions must cover every approved case exactly once")
    expected_count = predicted_count = matched_names = correct_sentiments = strict_docs = 0
    for case in golden.cases:
        predicted = predicted_cases[case.case_id]
        if predicted.input_sha256 != case.input_sha256:
            raise ValueError("prediction input digest differs from the approved case")
        expected = {label.perfume_id: label.sentiment for label in case.labels}
        actual = {label.perfume_id: label.sentiment for label in predicted.mentions}
        common = expected.keys() & actual.keys()
        expected_count += len(expected)
        predicted_count += len(actual)
        matched_names += len(common)
        correct_sentiments += sum(expected[identifier] == actual[identifier] for identifier in common)
        strict_docs += expected == actual
    precision = matched_names / predicted_count if predicted_count else 0.0
    recall = matched_names / expected_count
    f1 = 2 * matched_names / (expected_count + predicted_count)
    sentiment_accuracy = correct_sentiments / expected_count
    return EvaluationReport(
        dataset_id=golden.dataset_id, case_count=len(golden.cases),
        expected_mentions=expected_count, predicted_mentions=predicted_count,
        matched_names=matched_names, correct_sentiments=correct_sentiments,
        name_precision=precision, name_recall=recall, name_micro_f1=f1,
        sentiment_accuracy=sentiment_accuracy,
        strict_document_accuracy=strict_docs / len(golden.cases),
        accepted=f1 >= 0.90 and sentiment_accuracy >= 0.85,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        golden = GoldenDataset.model_validate_json(args.golden.read_text(encoding="utf-8"))
        predictions = PredictionSet.model_validate_json(args.predictions.read_text(encoding="utf-8"))
        report = evaluate(golden, predictions)
    except (OSError, ValueError, ValidationError):
        print(json.dumps({"status": "invalid_input", "accepted": False}))
        return 2
    print(report.model_dump_json())
    return 0 if report.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
