import logging
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal
from typing import Annotated, Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)


logger = logging.getLogger(__name__)

SourceKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ExternalId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Count = Annotated[int, Field(ge=0)]
UnitInterval = Annotated[float, Field(ge=0, le=1)]
NoteLevel = Literal["top", "heart", "base", "accord", "unspecified"]
Sentiment = Literal["positive", "neutral", "negative"]
OfferFormat = Literal["bottle", "miniature", "sample"]
FailureReason = Literal["adapter_error", "invalid_payload"]
source_key_type = TypeAdapter(SourceKey)
http_url_type = TypeAdapter(AnyHttpUrl)


def _validate_web_url(value: str) -> str:
    if "\\" in value or any(
        char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
    ):
        raise ValueError("URL contains whitespace, a control character, or a backslash")
    authority = urlsplit(value).netloc
    if not authority or "@" in authority:
        raise ValueError("URL requires a host and excludes user credentials")
    http_url_type.validate_python(value)
    return value


WebUrl = Annotated[
    str, StringConstraints(pattern=r"(?i)^https?://", max_length=2048), AfterValidator(_validate_web_url)
]


class SourceRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        revalidate_instances="always",
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class NoteRecord(SourceRecord):
    name: Label
    level: NoteLevel
    weight: UnitInterval | None = None


class PerfumeRecord(SourceRecord):
    external_id: ExternalId
    name: Label
    house: Label
    release_year: Annotated[int, Field(ge=1, le=9999)] | None = None
    perfumers: Annotated[list[Label], Field(min_length=1)] | None = None
    photo_url: WebUrl | None = None
    source_url: WebUrl | None = None
    positioning: Label | None = None
    notes: list[NoteRecord] = Field(default_factory=list)


class PerfumeObservation(SourceRecord):
    external_id: ExternalId
    perfume_external_id: ExternalId
    perfume_source_key: SourceKey | None = None
    source_url: WebUrl | None = None


class RatingRecord(PerfumeObservation):
    observed_at: AwareDatetime
    mean_rating: Annotated[float, Field(ge=0, le=5)] | None = None
    rating_count: Count | None = None
    positive_count: Count | None = None
    negative_count: Count | None = None
    popularity_rank: Annotated[int, Field(ge=1)] | None = None


class ReviewTextRecord(SourceRecord):
    external_id: ExternalId
    source_url: WebUrl
    published_at: AwareDatetime | None = None
    blogger_external_id: ExternalId | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class MentionRecord(PerfumeObservation):
    source_url: WebUrl
    published_at: AwareDatetime | None = None
    blogger_external_id: ExternalId | None = None
    sentiment: Sentiment
    evidence_quote: str | None = None
    confidence: UnitInterval | None = None


class OfferRecord(PerfumeObservation):
    source_url: WebUrl
    observed_at: AwareDatetime
    format: OfferFormat | None = None
    price: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)] | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    in_stock: bool | None = None

    @model_validator(mode="after")
    def require_price_currency_pair(self) -> Self:
        if (self.price is None) != (self.currency is None):
            raise ValueError("price and currency must be supplied together")
        return self


class DatasetRevisionRecord(SourceRecord):
    revision_ref: ExternalId
    artifact_url: WebUrl
    artifact_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SourceBatch(SourceRecord):
    source_key: SourceKey
    dataset_revision: DatasetRevisionRecord | None = None
    perfumes: list[PerfumeRecord] = Field(default_factory=list)
    ratings: list[RatingRecord] = Field(default_factory=list)
    review_texts: list[ReviewTextRecord] = Field(default_factory=list)
    mentions: list[MentionRecord] = Field(default_factory=list)
    offers: list[OfferRecord] = Field(default_factory=list)


class SourceAdapter(Protocol):
    source_key: str
    collect: Callable[[], Awaitable[SourceBatch]]


class SourceSuccess(SourceRecord):
    source_key: SourceKey
    status: Literal["ok"] = "ok"
    batch: SourceBatch


class SourceFailure(SourceRecord):
    source_key: SourceKey | None
    status: Literal["failed"] = "failed"
    reason: FailureReason


def _failure(source_key: str | None, reason: FailureReason) -> SourceFailure:
    logger.warning("Source collection failed (%s)", reason)
    return SourceFailure(source_key=source_key, reason=reason)


async def collect_source(adapter: SourceAdapter) -> SourceSuccess | SourceFailure:
    source_key = None
    try:
        source_key = source_key_type.validate_python(adapter.source_key, strict=True)
        batch = SourceBatch.model_validate(await adapter.collect())
    except ValidationError:
        return _failure(source_key, "invalid_payload")
    except Exception:
        return _failure(source_key, "adapter_error")

    if batch.source_key != source_key:
        return _failure(source_key, "invalid_payload")
    return SourceSuccess(source_key=source_key, batch=batch)


async def collect_sources(adapters: Iterable[SourceAdapter]) -> list[SourceSuccess | SourceFailure]:
    """Collect only adapters the caller has approved and enabled."""
    return [await collect_source(adapter) for adapter in adapters]
