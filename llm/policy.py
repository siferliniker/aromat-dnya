"""Explicit content scope and rights supplied by the source admission boundary."""

from dataclasses import dataclass
from collections.abc import Sequence
from datetime import datetime
import re
from typing import Literal

from collectors.contracts import WebUrl
from pydantic import TypeAdapter, ValidationError

from llm.errors import AdmissionDenied, InvalidEvidence


@dataclass(frozen=True)
class SourceApproval:
    source_key: str
    approval_id: str
    enabled: bool
    permission_status: str
    allow_llm: bool
    scope: Literal["public_review", "manual_nonprivate"]
    approved_urls: Sequence[str] = ()
    obtained_at: datetime | None = None
    expires_at: datetime | None = None
    allow_quotation: bool = False
    allow_retention: bool = False
    max_age_seconds: int | None = None


def aware(value: datetime | None) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def require_fresh_observation(
    observed_at: datetime, *, now: datetime, configured_ttl: int | None,
    approval: SourceApproval,
) -> None:
    if (not aware(now) or not aware(observed_at) or observed_at > now
            or type(configured_ttl) is not int or configured_ttl <= 0):
        raise InvalidEvidence()
    ttl = configured_ttl
    if approval.max_age_seconds is not None:
        if type(approval.max_age_seconds) is not int or approval.max_age_seconds <= 0:
            raise InvalidEvidence()
        ttl = min(ttl, approval.max_age_seconds)
    if (now - observed_at).total_seconds() > ttl:
        raise InvalidEvidence()


def authorize(
    approval: SourceApproval | None, *, source_key: str, source_url: str,
    now: datetime, quotation: bool = False, approval_id: str | None = None,
) -> SourceApproval:
    if (not isinstance(approval, SourceApproval) or not aware(now)
            or approval.source_key != source_key or not approval.approval_id.strip()
            or approval.enabled is not True or approval.permission_status != "approved"
            or approval.allow_llm is not True or approval.allow_retention is not True
            or approval.scope not in ("public_review", "manual_nonprivate")
            or (quotation and approval.allow_quotation is not True)
            or (approval_id is not None and approval.approval_id != approval_id)
            or source_url not in approval.approved_urls):
        raise AdmissionDenied()
    try:
        TypeAdapter(WebUrl).validate_python(source_url, strict=True)
    except (ValidationError, TypeError):
        raise AdmissionDenied() from None
    if (approval.obtained_at is not None
            and (not aware(approval.obtained_at) or approval.obtained_at > now)):
        raise AdmissionDenied()
    if (approval.expires_at is not None
            and (not aware(approval.expires_at) or approval.expires_at <= now)):
        raise AdmissionDenied()
    if (approval.max_age_seconds is not None
            and (type(approval.max_age_seconds) is not int or approval.max_age_seconds <= 0)):
        raise AdmissionDenied()
    return approval


_PRIVATE_MARKERS = re.compile(
    r"(?i)(?:\b(?:user_id|chat_id|telegram_chat_id|telegram_bot_token|openai_api_key)\b"
    r"|\b(?:authorization\s*:\s*bearer|private\s+chat)\b"
    r"|\bsk-[a-z0-9_-]{12,}|\b\d{5,}:[a-z0-9_-]{20,})"
)


def require_nonprivate_text(text: str) -> None:
    """Defense in depth; source consent must already attest to nonprivate content."""
    if not isinstance(text, str) or _PRIVATE_MARKERS.search(text):
        raise AdmissionDenied()
