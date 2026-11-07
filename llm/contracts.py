import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class StructuredResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ExtractedMention(StructuredResponse):
    perfume_name: Annotated[str, Field(min_length=1)]
    perfume_house: Annotated[str, Field(min_length=1)] | None
    sentiment: Literal["positive", "neutral", "negative"]
    evidence_quote: Annotated[str, Field(min_length=1)]
    confidence: Annotated[float, Field(ge=0, le=1)] | None


class ReviewExtraction(StructuredResponse):
    mentions: list[ExtractedMention]


class CardSummary(StructuredResponse):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    evidence_mention_ids: Annotated[list[Annotated[int, Field(gt=0)]], Field(min_length=1)]


def build_cache_key(
    *,
    task: Literal["extraction", "summary"],
    model: str,
    prompt_version: str,
    schema_version: str,
    input_text: str,
) -> str:
    dimensions = ["openai", task, model, prompt_version, schema_version, input_text]
    payload = json.dumps(dimensions, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
