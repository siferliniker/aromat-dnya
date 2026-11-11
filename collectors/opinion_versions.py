"""Fetch timestamps accompany review records outside the frozen batch DTO."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from collectors.contracts import ReviewTextRecord


@dataclass(frozen=True)
class ReviewSnapshot:
    review: ReviewTextRecord
    observed_at: datetime
    approval_ref: str
    approved_at: datetime


@dataclass(frozen=True)
class CollectedReviews:
    source_key: str
    snapshots: Sequence[ReviewSnapshot]
