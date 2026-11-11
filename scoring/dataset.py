from collections.abc import Sequence
from dataclasses import dataclass

from config import ScoringConfig
from storage.models import RatingObservation


COMPONENT_NAMES = (
    "fragrantica", "russian_reviews", "bloggers", "consensus", "novelty", "hype", "taste"
)


@dataclass(frozen=True)
class ComponentScore:
    name: str
    value: float
    effective_weight: float
    rating_observation_ids: Sequence[int]


@dataclass(frozen=True)
class ScoreResult:
    total: float | None
    components: Sequence[ComponentScore]
    missing: Sequence[str]
    algorithm_version: str = "dataset-v2"


def score_dataset(rating: RatingObservation | None, config: ScoringConfig, *,
                  taste: float | None = None, rank_max: int | None = None) -> ScoreResult:
    """Scento rank is an ordinal popularity signal, never a rating or vote count."""
    available: dict[str, float] = {}
    if taste is not None:
        available["taste"] = max(0.0, min(1.0, taste))
    if rating is not None:
        count, rank = rating.rating_count, rating.popularity_rank
        if count is not None:
            cutoff = config.thresholds.novelty_max_rating_count
            available["novelty"] = max(0.0, 1 - count / cutoff) if cutoff else float(count == 0)
        if count is not None or rank is not None:
            count_signal = min(1.0, count / config.thresholds.hype_min_rating_count) if count is not None else None
            rank_signal = (max(0.0, 1 - (rank - 1) / (rank_max - 1)) if rank_max and rank_max > 1
                           else config.thresholds.hype_max_popularity_rank /
                           (config.thresholds.hype_max_popularity_rank + rank)) if rank is not None else None
            available["hype"] = max(v for v in (count_signal, rank_signal) if v is not None)
    weights = config.weights.model_dump()
    denominator = sum(weights[name] for name in available if weights[name] > 0)
    missing = tuple(name for name in COMPONENT_NAMES if name not in available)
    if not denominator:
        return ScoreResult(None, (), missing)
    evidence = (rating.id,) if rating is not None and rating.id is not None else ()
    components = tuple(
        ComponentScore(
            name, value,
            weights[name] / denominator * (config.hype_sign if name == "hype" else 1),
            () if name == "taste" else evidence,
        )
        for name, value in available.items() if weights[name] > 0
    )
    return ScoreResult(sum(c.value * c.effective_weight for c in components), components, missing, "dataset-v2")
