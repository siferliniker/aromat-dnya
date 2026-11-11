"""Resolve catalog identities and render only verbatim, authorized evidence."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal
import unicodedata

from pydantic import ValidationError

from collectors.contracts import ReviewTextRecord
from llm.contracts import CardSummary, ExtractedMention, ReviewExtraction
from llm.errors import InvalidEvidence, InvalidModelResponse
from llm.policy import SourceApproval, authorize, aware, require_fresh_observation, require_nonprivate_text


@dataclass(frozen=True)
class CatalogPerfume:
    id: int
    name: str
    house: str
    source_key: str
    external_id: str
    aliases: Sequence[str] = ()


@dataclass(frozen=True)
class ResolvedMention:
    perfume_id: int
    catalog_source_key: str
    perfume_external_id: str
    mention: ExtractedMention


@dataclass(frozen=True)
class QuarantinedMention:
    index: int
    reason: Literal["invalid_quote", "unknown_perfume", "ambiguous_perfume", "conflicting_sentiment"]


@dataclass(frozen=True)
class ExtractionResult:
    mentions: Sequence[ResolvedMention]
    quarantine: Sequence[QuarantinedMention]


@dataclass(frozen=True)
class SummaryEvidence:
    mention_id: int
    perfume_id: int
    source_key: str
    source_url: str
    sentiment: Literal["positive", "neutral", "negative"]
    quote: str
    published_at: datetime | None
    approval_id: str
    observed_at: datetime


def _canonical(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _phrase_in(phrase: str, text: str) -> bool:
    phrase = _canonical(phrase)
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", _canonical(text)))


def _names(perfume: CatalogPerfume) -> Sequence[str]:
    if (not isinstance(perfume.aliases, Sequence) or isinstance(perfume.aliases, str)
            or any(not isinstance(alias, str) or not alias.strip() for alias in perfume.aliases)):
        raise InvalidEvidence()
    return (perfume.name, *perfume.aliases)


def validate_extraction(
    result: ReviewExtraction, *, review: ReviewTextRecord, catalog: Sequence[CatalogPerfume],
) -> ExtractionResult:
    try:
        result = ReviewExtraction.model_validate_json(result.model_dump_json())
        review = ReviewTextRecord.model_validate(review)
    except (ValidationError, AttributeError, TypeError):
        raise InvalidModelResponse() from None
    accepted, quarantine = [], []
    for index, item in enumerate(result.mentions):
        if not item.evidence_quote.strip() or item.evidence_quote not in review.text:
            quarantine.append(QuarantinedMention(index, "invalid_quote"))
            continue
        matches = [perfume for perfume in catalog
                   if any(_canonical(name) == _canonical(item.perfume_name) for name in _names(perfume))
                   and (item.perfume_house is None
                        or _canonical(perfume.house) == _canonical(item.perfume_house))]
        if len(matches) != 1:
            quarantine.append(QuarantinedMention(
                index, "unknown_perfume" if not matches else "ambiguous_perfume",
            ))
            continue
        perfume = matches[0]
        if (not any(_phrase_in(name, item.evidence_quote) for name in _names(perfume))
                or (item.perfume_house is not None
                    and not _phrase_in(item.perfume_house, review.text))):
            quarantine.append(QuarantinedMention(index, "invalid_quote"))
            continue
        if type(perfume.id) is not int or perfume.id <= 0:
            raise InvalidEvidence()
        accepted.append((index, ResolvedMention(perfume.id, perfume.source_key, perfume.external_id, item)))
    sentiments: dict[int, set[str]] = {}
    for _, resolved in accepted:
        sentiments.setdefault(resolved.perfume_id, set()).add(resolved.mention.sentiment)
    unique: dict[int, ResolvedMention] = {}
    for index, resolved in accepted:
        if len(sentiments[resolved.perfume_id]) > 1:
            quarantine.append(QuarantinedMention(index, "conflicting_sentiment"))
        else:
            unique.setdefault(resolved.perfume_id, resolved)
    return ExtractionResult(tuple(unique.values()), tuple(sorted(quarantine, key=lambda item: item.index)))


def extraction_is_cacheable(
    result: ReviewExtraction, checked: ExtractionResult, *, review: ReviewTextRecord,
) -> bool:
    for item in checked.quarantine:
        if item.reason == "invalid_quote":
            return False
        if item.reason in ("unknown_perfume", "ambiguous_perfume"):
            mention = result.mentions[item.index]
            if (not _phrase_in(mention.perfume_name, mention.evidence_quote)
                    or (mention.perfume_house is not None
                        and not _phrase_in(mention.perfume_house, review.text))):
                return False
    return True


_LABELS = {"positive": "Положительно", "neutral": "Нейтрально", "negative": "Отрицательно"}


def render_summary(evidence: Sequence[SummaryEvidence]) -> str:
    if not 1 <= len(evidence) <= 2:
        raise InvalidEvidence()
    lines = ["Подтверждённые мнения:"]
    for item in evidence:
        if (type(item.mention_id) is not int or item.mention_id <= 0
                or item.sentiment not in _LABELS or not isinstance(item.quote, str)
                or not item.quote.strip()
                or any(unicodedata.category(char).startswith("C")
                       or char in "\u2028\u2029" for char in item.quote)):
            raise InvalidEvidence()
        lines.append(f"{_LABELS[item.sentiment]}: «{item.quote}» [m{item.mention_id}]")
    return "\n".join(lines)


def authorize_evidence(
    item: SummaryEvidence, *, perfume_id: int, approvals: Mapping[str, SourceApproval],
    now: datetime, freshness_seconds: Mapping[str, int] | None,
) -> None:
    if (not isinstance(item, SummaryEvidence) or type(item.mention_id) is not int
            or item.mention_id <= 0 or item.perfume_id != perfume_id
            or not aware(item.observed_at) or item.observed_at > now):
        raise InvalidEvidence()
    approval = authorize(
        approvals.get(item.source_key), source_key=item.source_key, source_url=item.source_url,
        now=now, quotation=True, approval_id=item.approval_id,
    )
    require_fresh_observation(
        item.observed_at, now=now, configured_ttl=(freshness_seconds or {}).get(item.source_key),
        approval=approval,
    )
    if item.published_at is not None and not aware(item.published_at):
        raise InvalidEvidence()
    require_nonprivate_text(item.quote)


def validate_summary(
    result: CardSummary, *, perfume_id: int, evidence: Sequence[SummaryEvidence],
    approvals: Mapping[str, SourceApproval], now: datetime, max_utf16_units: int,
    freshness_seconds: Mapping[str, int] | None = None,
) -> CardSummary:
    try:
        result = CardSummary.model_validate_json(result.model_dump_json())
    except (ValidationError, AttributeError, TypeError):
        raise InvalidEvidence() from None
    identifiers = result.evidence_mention_ids
    if (not 1 <= len(identifiers) <= 2 or len(set(identifiers)) != len(identifiers)
            or type(max_utf16_units) is not int or not 0 < max_utf16_units <= 4096
            or not aware(now)):
        raise InvalidEvidence()
    by_id = {item.mention_id: item for item in evidence}
    if len(by_id) != len(evidence) or any(identifier not in by_id for identifier in identifiers):
        raise InvalidEvidence()
    selected = [by_id[identifier] for identifier in identifiers]
    for item in selected:
        authorize_evidence(item, perfume_id=perfume_id, approvals=approvals, now=now,
                           freshness_seconds=freshness_seconds)
    rendered = render_summary(selected)
    if result.text != rendered or len(rendered.encode("utf-16-le")) // 2 > max_utf16_units:
        raise InvalidEvidence()
    return result
