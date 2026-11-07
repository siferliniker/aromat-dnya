from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


UnitInterval = Annotated[float, Field(ge=0, le=1)]


class ScoreComponents(BaseModel):
    """Normalized inputs; None denotes a component without enough evidence."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    fragrantica: UnitInterval | None = None
    russian_reviews: UnitInterval | None = None
    bloggers: UnitInterval | None = None
    consensus: UnitInterval | None = None
    novelty: UnitInterval | None = None
    hype: UnitInterval | None = None
    taste: UnitInterval | None = None
