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
    algorithm_version: str = "dataset-v1"


def score_dataset(rating: RatingObservation | None, config: ScoringConfig) -> ScoreResult:
    """Scento rank is an ordinal popularity signal, never a rating or vote count."""
    available: dict[str, float] = {}
    if rating is not None:
        count, rank = rating.rating_count, rating.popularity_rank
        if count is not None:
            cutoff = config.thresholds.novelty_max_rating_count
            available["novelty"] = max(0.0, 1 - count / cutoff) if cutoff else float(count == 0)
        if count is not None or rank is not None:
            available["hype"] = float(
                (count is not None and count >= config.thresholds.hype_min_rating_count)
                or (rank is not None and rank <= config.thresholds.hype_max_popularity_rank)
            )
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
            evidence,
        )
        for name, value in available.items() if weights[name] > 0
    )
    return ScoreResult(sum(c.value * c.effective_weight for c in components), components, missing)
