"""LLM errors. Messages never contain prompt text, model output or credentials."""

from __future__ import annotations


class LLMError(Exception):
    """Base class. `outcome` is the value recorded in `llm_calls.outcome`."""

    outcome = "error"


class LLMNotConfiguredError(LLMError):
    """The selected provider cannot run (e.g. `anthropic` without an API key, unknown price)."""

    outcome = "not_configured"


class LLMInvalidOutputError(LLMError):
    """Output failed schema validation on every allowed attempt (bounded retry exhausted)."""

    outcome = "invalid_output"


class LLMTimeoutError(LLMError):
    """A call exceeded its timeout on every allowed attempt."""

    outcome = "timeout"


class LLMTransportError(LLMError):
    """429 / 5xx / connection failure on every allowed attempt."""

    outcome = "transport_error"


class LLMFatalError(LLMError):
    """A non-retryable provider error (400, 401, 403, 404, ...)."""

    outcome = "fatal_error"


class LLMRefusalError(LLMError):
    """The model (and any server-side fallback) declined the request."""

    outcome = "refusal"


class LLMInputTooLargeError(LLMError):
    """The estimated prompt exceeds LLM_MAX_INPUT_TOKENS. Nothing was sent; the caller must
    pre-reduce further or end the stage as incomplete."""

    outcome = "input_too_large"

    def __init__(self, estimated: int, limit: int) -> None:
        super().__init__(f"estimated prompt {estimated} tokens > limit {limit}")
        self.estimated = estimated
        self.limit = limit


class LLMBudgetExceededError(LLMError):
    """The caller's pre-call budget hook refused the call. No request was sent."""

    outcome = "budget_exceeded"

    def __init__(self, budget: str) -> None:
        super().__init__(f"budget exhausted: {budget}")
        self.budget = budget


# Raised by providers; the client maps them to the errors above after its retry policy.
class ProviderError(Exception):
    def __init__(self, error_class: str, *, retryable: bool) -> None:
        super().__init__(error_class)
        self.error_class = error_class
        self.retryable = retryable
