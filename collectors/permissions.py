"""Explicit, fail-closed source admission supplied by the operator's rights review."""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from collectors.contracts import SourceKey, WebUrl
from llm.policy import SourceApproval, aware


Nonempty = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Operation = Literal["fetch", "store", "llm", "derive", "quote", "golden"]


class PermissionDenied(ValueError):
    """The current registry does not admit this operation."""


class AdmissionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False,
                              hide_input_in_errors=True, frozen=True, revalidate_instances="always")


class Operations(AdmissionModel):
    fetch: bool
    store: bool
    llm: bool
    derive: bool
    quote: bool
    golden: bool


class BloggerAdmission(AdmissionModel):
    external_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    channel_url: WebUrl
    weight: Annotated[float, Field(ge=0, le=1)]


class SourceAdmission(AdmissionModel):
    source_key: SourceKey
    kind: Literal["review", "blog", "manual"]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    platform: SourceKey
    enabled: bool
    permission_status: Literal["pending", "approved", "blocked"]
    terms_url: WebUrl
    scope: Literal["public_review", "manual_nonprivate"]
    approval_ref: Nonempty | None
    platform_approval_ref: Nonempty | None
    author_approval_ref: Nonempty | None
    approved_at: AwareDatetime | None
    expires_at: AwareDatetime | None
    operations: Operations
    approved_urls: list[WebUrl]
    bloggers: list[BloggerAdmission]
    evidence_role: Literal["fragrantica", "russian_reviews", "bloggers", "popularity"]
    max_age_seconds: Annotated[int, Field(gt=0)] | None

    @model_validator(mode="after")
    def require_matching_scope(self) -> Self:
        if self.enabled and self.permission_status == "approved" and (
            any(value is None for value in (
                self.approval_ref, self.platform_approval_ref, self.author_approval_ref, self.approved_at,
            )) or not self.approved_urls
        ):
            raise ValueError("enabled admission requires separate platform and author attestations")
        if self.expires_at is not None and self.approved_at is not None and self.expires_at <= self.approved_at:
            raise ValueError("permission expiry must follow approval")
        if len(self.approved_urls) != len(set(self.approved_urls)):
            raise ValueError("duplicate admitted content URL")
        if len(self.bloggers) != len({value.external_id for value in self.bloggers}):
            raise ValueError("duplicate blogger identity")
        if self.kind == "manual" and (self.scope != "manual_nonprivate" or self.evidence_role != "popularity"):
            raise ValueError("manual content cannot be relabelled as a review platform")
        if self.kind == "blog" and self.evidence_role != "bloggers":
            raise ValueError("blog content requires the blogger evidence role")
        if self.evidence_role == "bloggers" and self.kind != "blog":
            raise ValueError("blogger evidence requires a blog source")
        if self.evidence_role == "fragrantica" and (
            self.source_key != "fragrantica_live" or self.platform != "fragrantica_live" or self.kind != "review"
        ):
            raise ValueError("Fragrantica evidence requires its explicitly admitted source")
        if self.kind != "blog" and self.bloggers:
            raise ValueError("blogger identities belong to blog sources")
        return self


class AdmissionRegistry:
    def __init__(self, admissions: Sequence[SourceAdmission]):
        self.admissions = {}
        for value in admissions:
            value = SourceAdmission.model_validate(SourceAdmission.model_validate(value).model_dump(mode="python"))
            if value.source_key in self.admissions:
                raise ValueError("duplicate admission source identity")
            self.admissions[value.source_key] = value

    def require(
        self, source_key: str, operation: Operation, *, now: datetime,
        source_url: str | None = None, blogger_external_id: str | None = None,
    ) -> SourceAdmission:
        admission = self.admissions.get(source_key)
        if (admission is None or not aware(now) or operation not in Operations.model_fields
                or not admission.enabled or admission.permission_status != "approved"
                or not getattr(admission.operations, operation)
                or admission.approved_at is None or admission.approved_at > now
                or (admission.expires_at is not None and admission.expires_at <= now)
                or (source_url is not None and source_url not in admission.approved_urls)
                or (blogger_external_id is not None and blogger_external_id not in {
                    item.external_id for item in admission.bloggers
                })):
            raise PermissionDenied("source operation has not been admitted")
        # Callers cannot widen the live registry by editing returned list values.
        return SourceAdmission.model_validate(admission.model_dump(mode="python"))

    def to_llm_approval(self, source_key: str, *, now: datetime) -> SourceApproval:
        value = self.require(source_key, "llm", now=now)
        self.require(source_key, "store", now=now)
        return SourceApproval(
            source_key=value.source_key, approval_id=value.approval_ref, enabled=value.enabled,
            permission_status=value.permission_status, allow_llm=value.operations.llm,
            scope=value.scope, approved_urls=tuple(value.approved_urls), obtained_at=value.approved_at,
            expires_at=value.expires_at, allow_quotation=value.operations.quote,
            allow_retention=value.operations.store, max_age_seconds=value.max_age_seconds,
        )


class _AdmissionFile(AdmissionModel):
    sources: list[SourceAdmission]


def load_admission_registry(path: str | Path) -> AdmissionRegistry:
    payload = _AdmissionFile.model_validate_json(Path(path).read_text(encoding="utf-8"))
    return AdmissionRegistry(payload.sources)
