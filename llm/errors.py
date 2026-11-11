"""Public failures carry safe reason codes, never provider bodies or input text."""


class LLMError(RuntimeError):
    code = "llm_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class ProcessingDisabled(LLMError):
    code = "processing_disabled"


class AdmissionDenied(LLMError):
    code = "admission_denied"


class InvalidEvidence(LLMError):
    code = "invalid_evidence"


class InvalidModelResponse(LLMError):
    code = "invalid_model_response"


class ModelRefusal(InvalidModelResponse):
    code = "model_refusal"


class IncompleteResponse(InvalidModelResponse):
    code = "incomplete_response"


class DuplicateClaim(LLMError):
    code = "duplicate_claim_requires_operator_review"


class UncertainOutcome(LLMError):
    code = "uncertain_outcome_requires_operator_review"


class RequestRejected(LLMError):
    """A confirmed rejection before generation; only explicit transient errors retry."""

    code = "request_rejected_before_acceptance"

    def __init__(self, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__()
