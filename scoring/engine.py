"""Pure opinion scoring over approved, fresh evidence selected by the caller.

The engine does not fetch, approve, cache or persist evidence. Cumulative
Fragrantica snapshots are never added: the latest observation wins globally.
Popularity is current per source; novelty uses the latest known actual count,
while hype considers all current sources' independent count and rank signals.
Ties between rating snapshots use their persisted IDs, then source keys.

Mentions are counted once per ID and, when provided, per source/document key.
Only the latest document version contributes. Contradictory interpretations
of the same document at the same observation time are rejected. A document
key identifies the document across versions, not its individual version.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from math import fsum, isclose, isfinite
from numbers import Real
import re
from typing import Literal

from config import ScoringConfig


COMPONENT_NAMES = (
    "fragrantica", "russian_reviews", "bloggers", "consensus", "novelty", "hype", "taste",
)
_SENTIMENT = {"positive": 1.0, "neutral": 0.5, "negative": 0.0}
Disagreement = Literal["fragrantica_higher", "russian_reviews_higher"]


@dataclass(frozen=True)
class RatingEvidence:
    id: int
    source_key: str
    mean_rating: float | None
    rating_count: int | None
    positive_count: int | None
    negative_count: int | None
    popularity_rank: int | None
    observed_at: datetime


@dataclass(frozen=True)
class MentionEvidence:
    id: int
    source_key: str
    blogger_id: int | None
    sentiment: str
    confidence: float | None
    applied_blogger_weight: float | None
    observed_at: datetime
    published_at: datetime | None
    document_key: str | None = None


@dataclass(frozen=True)
class ScoringInputs:
    fragrantica_ratings: Sequence[RatingEvidence]
    russian_mentions: Sequence[MentionEvidence]
    blogger_mentions: Sequence[MentionEvidence]
    popularity_observations: Sequence[RatingEvidence]


@dataclass(frozen=True)
class EvidenceRef:
    rating_observation_id: int | None = None
    mention_id: int | None = None
    applied_blogger_weight: float | None = None

    def __post_init__(self) -> None:
        if (self.rating_observation_id is None) == (self.mention_id is None):
            raise ValueError("evidence must reference exactly one persisted input")
        if self.rating_observation_id is not None:
            _require_int(self.rating_observation_id, "rating_observation_id", 1)
        if self.mention_id is not None:
            _require_int(self.mention_id, "mention_id", 1)
        if self.applied_blogger_weight is not None:
            _require_number(self.applied_blogger_weight, "applied_blogger_weight", 1)
            if self.mention_id is None:
                raise ValueError("a blogger weight requires a mention reference")


@dataclass(frozen=True)
class OpinionComponent:
    name: str
    value: float
    effective_weight: float
    evidence: Sequence[EvidenceRef]


@dataclass(frozen=True)
class OpinionScore:
    total: float | None
    components: Sequence[OpinionComponent]
    missing: Sequence[str]
    disagreement: Disagreement | None
    algorithm_version: str = "opinions-v1"


def _require_int(value: object, field: str, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer at least {minimum}")


def _require_number(value: object, field: str, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite number in range")
    if not 0 <= value <= maximum or not isfinite(value):
        raise ValueError(f"{field} must be a finite number in range")


def _require_datetime(value: object, field: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")


def _validate_identity(value: RatingEvidence | MentionEvidence) -> None:
    _require_int(value.id, "id", 1)
    if not isinstance(value.source_key, str) or re.fullmatch(
        r"[a-z][a-z0-9_-]{0,63}", value.source_key,
    ) is None:
        raise ValueError("source_key must be a valid configured source key")
    _require_datetime(value.observed_at, "observed_at")


def _validate_rating(value: RatingEvidence) -> None:
    _validate_identity(value)
    if value.mean_rating is not None:
        _require_number(value.mean_rating, "mean_rating", 5)
    for field in ("rating_count", "positive_count", "negative_count", "popularity_rank"):
        count = getattr(value, field)
        if count is not None:
            _require_int(count, field, 1 if field == "popularity_rank" else 0)


def _validate_mention(value: MentionEvidence, *, blogger: bool) -> None:
    _validate_identity(value)
    if not isinstance(value.sentiment, str) or value.sentiment not in _SENTIMENT:
        raise ValueError("sentiment must be positive, neutral or negative")
    if value.confidence is not None:
        _require_number(value.confidence, "confidence", 1)
    if value.published_at is not None:
        _require_datetime(value.published_at, "published_at")
    if value.document_key is not None and (
        not isinstance(value.document_key, str) or not value.document_key.strip()
    ):
        raise ValueError("document_key must be a nonempty string when supplied")
    if value.blogger_id is not None:
        _require_int(value.blogger_id, "blogger_id", 1)
    if value.applied_blogger_weight is not None:
        _require_number(value.applied_blogger_weight, "applied_blogger_weight", 1)
        if value.blogger_id is None:
            raise ValueError("a blogger weight requires an explicit blogger identity")
    if blogger and (value.blogger_id is None or value.applied_blogger_weight is None):
        raise ValueError("blogger opinions require explicit identity and trust")


def _sequence(value: object, element_type: type, field: str) -> tuple:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a sequence of evidence")
    snapshot = tuple(value)
    if any(not isinstance(item, element_type) for item in snapshot):
        raise ValueError(f"{field} contains an invalid evidence type")
    return snapshot


def _deduplicate_ids(values: Sequence) -> tuple:
    unique = {}
    for value in values:
        if value.id in unique and unique[value.id] != value:
            raise ValueError("a persisted evidence ID has conflicting payloads")
        unique[value.id] = value
    return tuple(unique[key] for key in sorted(unique))


def _current_mentions(values: Sequence[MentionEvidence]) -> Sequence[MentionEvidence]:
    documents: dict[tuple[str, str], MentionEvidence] = {}
    versions: dict[tuple[str, str, datetime], tuple] = {}
    without_document = []
    for value in _deduplicate_ids(values):
        if value.document_key is None:
            without_document.append(value)
            continue
        key = (value.source_key, value.document_key)
        version = (*key, value.observed_at)
        interpretation = (value.sentiment, value.blogger_id, value.applied_blogger_weight)
        if version in versions and versions[version] != interpretation:
            raise ValueError("a document version has conflicting opinions")
        versions[version] = interpretation
        if key not in documents or _latest_key(value) > _latest_key(documents[key]):
            documents[key] = value
    return tuple(sorted((*without_document, *documents.values()), key=lambda value: value.id))


def _latest_key(value: RatingEvidence | MentionEvidence) -> tuple[datetime, int, str]:
    return value.observed_at, value.id, value.source_key


def _fragrantica(value: RatingEvidence, config: ScoringConfig) -> float | None:
    policy = config.opinion_components
    if policy is None:
        raise ValueError("opinion_components is required for opinion scoring")
    prior = config.bayesian
    signals = []
    if value.mean_rating is not None and value.rating_count is not None and value.rating_count > 0:
        volume = value.rating_count + prior.prior_count
        rating = (
            value.rating_count / volume * (value.mean_rating / 5)
            + prior.prior_count / volume * (prior.rating_prior_mean / 5)
        )
        if policy.fragrantica_rating_share > 0:
            signals.append((rating, policy.fragrantica_rating_share))
    if value.positive_count is not None and value.negative_count is not None:
        reactions = value.positive_count + value.negative_count
        if reactions > 0 and policy.fragrantica_rating_share < 1:
            volume = reactions + prior.prior_count
            positive = (
                value.positive_count / volume
                + prior.prior_count / volume * prior.reaction_prior_positive
            )
            signals.append((positive, 1 - policy.fragrantica_rating_share))
    denominator = fsum(weight for _, weight in signals)
    if not denominator:
        return None
    return fsum(score * (weight / denominator) for score, weight in signals)


def score_opinions(inputs: ScoringInputs, config: ScoringConfig, *,
                   taste: float | None = None) -> OpinionScore:
    """Return deterministic scores and persisted evidence references, without I/O.

    A null count is unknown, not zero. Partial rating/reaction pairs therefore
    do not create F signals. Rating counts and reaction counts are independent.
    Confidence is validated provenance, never review volume or blogger trust.
    Available components with zero base weights are omitted from components
    but are not reported as missing, matching the dataset scoring contract.
    """
    if not isinstance(inputs, ScoringInputs):
        raise ValueError("inputs must be ScoringInputs")
    if config.opinion_components is None:
        raise ValueError("opinion_components is required for opinion scoring")
    ratings = _sequence(inputs.fragrantica_ratings, RatingEvidence, "fragrantica_ratings")
    popularity = _sequence(inputs.popularity_observations, RatingEvidence, "popularity_observations")
    russian = _sequence(inputs.russian_mentions, MentionEvidence, "russian_mentions")
    bloggers = _sequence(inputs.blogger_mentions, MentionEvidence, "blogger_mentions")
    for value in (*ratings, *popularity):
        _validate_rating(value)
    for value in russian:
        _validate_mention(value, blogger=False)
    for value in bloggers:
        _validate_mention(value, blogger=True)
    _deduplicate_ids((*ratings, *popularity))
    _deduplicate_ids((*russian, *bloggers))
    if {value.id for value in russian} & {value.id for value in bloggers}:
        raise ValueError("a mention cannot belong to both review and blogger channels")
    russian = _current_mentions(russian)
    bloggers = _current_mentions(bloggers)

    available: dict[str, tuple[float, Sequence[EvidenceRef]]] = {}
    if taste is not None:
        available["taste"] = max(0.0, min(1.0, taste)), ()
    if ratings:
        latest = max(ratings, key=_latest_key)
        fragrantica = _fragrantica(latest, config)
        if fragrantica is not None:
            available["fragrantica"] = fragrantica, (EvidenceRef(rating_observation_id=latest.id),)
    if russian:
        policy = config.opinion_components
        count = len(russian)
        volume = count + policy.russian_review_prior_count
        review_mean = fsum(_SENTIMENT[value.sentiment] for value in russian) / count
        value = (
            count / volume * review_mean
            + policy.russian_review_prior_count / volume * policy.russian_review_prior_mean
        )
        available["russian_reviews"] = value, tuple(EvidenceRef(mention_id=item.id) for item in russian)
    if bloggers:
        authors: dict[int, list[MentionEvidence]] = {}
        for value in bloggers:
            authors.setdefault(value.blogger_id, []).append(value)
        weighted_means = []
        refs = []
        for author_id in sorted(authors):
            mentions = authors[author_id]
            weight = mentions[0].applied_blogger_weight
            if any(value.applied_blogger_weight != weight for value in mentions):
                raise ValueError("a blogger has inconsistent applied trust weights")
            if weight > 0:
                mean = fsum(_SENTIMENT[value.sentiment] for value in mentions) / len(mentions)
                weighted_means.append((mean, weight))
                refs.extend(EvidenceRef(mention_id=value.id, applied_blogger_weight=weight) for value in mentions)
        denominator = fsum(weight for _, weight in weighted_means)
        if denominator:
            available["bloggers"] = (
                fsum(mean * (weight / denominator) for mean, weight in weighted_means),
                tuple(sorted(refs, key=lambda value: value.mention_id)),
            )
    disagreement = None
    if "fragrantica" in available and "russian_reviews" in available:
        fragrantica, rating_refs = available["fragrantica"]
        reviews, review_refs = available["russian_reviews"]
        gap = abs(fragrantica - reviews)
        threshold = config.thresholds.consensus_max_gap
        # Preserve the inclusive decimal boundary after binary floating-point
        # arithmetic. No absolute tolerance: a zero gap policy stays exact.
        agrees = gap <= threshold or isclose(gap, threshold, rel_tol=1e-12, abs_tol=0.0)
        available["consensus"] = float(agrees), (*rating_refs, *review_refs)
        if not agrees:
            disagreement = "fragrantica_higher" if fragrantica > reviews else "russian_reviews_higher"

    current: dict[str, RatingEvidence] = {}
    for value in popularity:
        if value.source_key not in current or _latest_key(value) > _latest_key(current[value.source_key]):
            current[value.source_key] = value
    counts = [value for value in current.values() if value.rating_count is not None]
    if counts:
        latest = max(counts, key=_latest_key)
        cutoff = config.thresholds.novelty_max_rating_count
        novelty = max(0.0, 1 - latest.rating_count / cutoff) if cutoff else float(latest.rating_count == 0)
        available["novelty"] = novelty, (EvidenceRef(rating_observation_id=latest.id),)
    known = sorted(
        (value for value in current.values() if value.rating_count is not None or value.popularity_rank is not None),
        key=lambda value: value.id,
    )
    if known:
        hype = any(
            (value.rating_count is not None and value.rating_count >= config.thresholds.hype_min_rating_count)
            or (value.popularity_rank is not None and value.popularity_rank <= config.thresholds.hype_max_popularity_rank)
            for value in known
        )
        available["hype"] = float(hype), tuple(EvidenceRef(rating_observation_id=value.id) for value in known)

    weights = config.weights.model_dump()
    denominator = fsum(weights[name] for name in available if weights[name] > 0)
    missing = tuple(name for name in COMPONENT_NAMES if name not in available)
    if not denominator:
        return OpinionScore(None, (), missing, disagreement)
    components = tuple(
        OpinionComponent(
            name, available[name][0],
            weights[name] / denominator * (config.hype_sign if name == "hype" else 1),
            available[name][1],
        )
        for name in COMPONENT_NAMES if name in available and weights[name] > 0
    )
    total = fsum(component.value * component.effective_weight for component in components)
    return OpinionScore(total, components, missing, disagreement,
                        "opinions-v2" if taste is not None else "opinions-v1")
