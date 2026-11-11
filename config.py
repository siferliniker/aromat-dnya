from datetime import time
from pathlib import Path
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


UnitInterval = Annotated[float, Field(strict=True, ge=0, le=1)]
NonnegativeCount = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]
SourceKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonemptyPath = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)
]
PositiveSeconds = Annotated[float, Field(strict=True, gt=0)]
NonnegativeSeconds = Annotated[float, Field(strict=True, ge=0)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class DeliveryConfig(ConfigModel):
    timezone: str
    send_at: time
    language: Literal["ru"]

    @field_validator("timezone")
    @classmethod
    def require_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("invalid IANA timezone") from error
        return value

    @field_validator("send_at")
    @classmethod
    def require_local_time(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("send_at must be a local wall-clock time")
        return value


class RecommendationConfig(ConfigModel):
    exploration_ratio: UnitInterval


class ScoringWeights(ConfigModel):
    fragrantica: UnitInterval
    russian_reviews: UnitInterval
    bloggers: UnitInterval
    consensus: UnitInterval
    novelty: UnitInterval
    hype: UnitInterval
    taste: UnitInterval

    @model_validator(mode="after")
    def require_active_component(self) -> Self:
        if not any(self.model_dump().values()):
            raise ValueError("at least one scoring weight must be positive")
        return self


class ComponentNormalization(ConfigModel):
    component_min: Literal[0]
    component_max: Literal[1]


class BayesianConfig(ConfigModel):
    rating_prior_mean: Annotated[float, Field(strict=True, ge=0, le=5)]
    reaction_prior_positive: UnitInterval
    prior_count: PositiveCount


class ScoringThresholds(ConfigModel):
    novelty_max_rating_count: NonnegativeCount
    hype_min_rating_count: PositiveCount
    hype_max_popularity_rank: PositiveCount
    consensus_max_gap: UnitInterval

    @model_validator(mode="after")
    def require_distinct_novelty_and_hype_ranges(self) -> Self:
        if self.novelty_max_rating_count >= self.hype_min_rating_count:
            raise ValueError("the novelty count cutoff must be below the hype count cutoff")
        return self


class OpinionComponentsConfig(ConfigModel):
    fragrantica_rating_share: UnitInterval
    russian_review_prior_mean: UnitInterval
    russian_review_prior_count: NonnegativeCount


class ScoringConfig(ConfigModel):
    weights: ScoringWeights
    hype_sign: Literal[-1]
    missing_data: Literal["redistribute_available"]
    normalization: ComponentNormalization
    bayesian: BayesianConfig
    thresholds: ScoringThresholds
    opinion_components: OpinionComponentsConfig | None = None


class FetchRuntimeConfig(ConfigModel):
    request_timeout_seconds: PositiveSeconds
    source_deadline_seconds: PositiveSeconds
    max_attempts: PositiveCount
    retry_backoff_seconds: list[NonnegativeSeconds]
    min_request_interval_seconds: NonnegativeSeconds
    max_items_per_source: PositiveCount

    @model_validator(mode="after")
    def require_bounded_retries(self) -> Self:
        if len(self.retry_backoff_seconds) != self.max_attempts - 1:
            raise ValueError("retry_backoff_seconds must provide one delay for each retry")
        if self.source_deadline_seconds < self.request_timeout_seconds:
            raise ValueError("source deadline must allow the configured request timeout")
        return self


class LLMRuntimeConfig(ConfigModel):
    request_timeout_seconds: PositiveSeconds
    max_attempts: PositiveCount
    retry_backoff_seconds: list[NonnegativeSeconds]
    max_input_chars: PositiveCount
    max_output_tokens: PositiveCount
    max_requests_per_run: PositiveCount
    max_concurrent_requests: PositiveCount

    @model_validator(mode="after")
    def require_bounded_retries(self) -> Self:
        if len(self.retry_backoff_seconds) != self.max_attempts - 1:
            raise ValueError("retry_backoff_seconds must provide one delay for each retry")
        return self


class Stage2RuntimeConfig(ConfigModel):
    enabled: Annotated[bool, Field(strict=True)]
    approvals_path: NonemptyPath | None = None
    fetch: FetchRuntimeConfig | None = None
    llm: LLMRuntimeConfig | None = None
    freshness_seconds: dict[SourceKey, PositiveCount] | None = None
    claims_path: NonemptyPath | None = None

    @model_validator(mode="after")
    def require_runtime_policy_when_enabled(self) -> Self:
        if self.enabled and any(value is None for value in (
            self.approvals_path, self.fetch, self.llm, self.freshness_seconds, self.claims_path,
        )):
            raise ValueError("enabled stage 2 requires approvals, fetch, LLM, freshness and claims policies")
        return self


class SourceConfig(ConfigModel):
    kind: Literal["dataset", "review", "blog", "shop", "manual"]
    enabled: Annotated[bool, Field(strict=True)]


class LLMConfig(ConfigModel):
    provider: Literal["openai"]
    enabled: Annotated[bool, Field(strict=True)]
    model: ModelName | None

    @model_validator(mode="after")
    def require_model_when_enabled(self) -> Self:
        if self.enabled and self.model is None:
            raise ValueError("select an OpenAI model before enabling LLM processing")
        return self


class AppConfig(ConfigModel):
    delivery: DeliveryConfig
    recommendation: RecommendationConfig
    scoring: ScoringConfig
    sources: dict[SourceKey, SourceConfig]
    llm: LLMConfig
    stage2: Stage2RuntimeConfig | None = None

    @model_validator(mode="after")
    def require_opinion_pipeline_dependencies(self) -> Self:
        if self.stage2 is None or not self.stage2.enabled:
            return self
        if not self.llm.enabled or self.llm.model is None:
            raise ValueError("enabled stage 2 requires an enabled OpenAI model")
        if self.scoring.opinion_components is None:
            raise ValueError("enabled stage 2 requires opinion scoring configuration")
        freshness = self.stage2.freshness_seconds or {}
        if any(key not in freshness for key, source in self.sources.items()
               if source.enabled and source.kind in ("review", "blog")):
            raise ValueError("enabled review and blog sources require explicit freshness limits")
        return self


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    with Path(path).open(encoding="utf-8") as config_file:
        return AppConfig.model_validate(yaml.safe_load(config_file))
