"""Fetch error taxonomy. ``error_class`` drives retry decisions, circuit breakers and agent playbooks."""

from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    HTTP_5XX = "HTTP_5XX"
    HTTP_429 = "HTTP_429"
    HTTP_403 = "HTTP_403"
    HTTP_404 = "HTTP_404"
    HTTP_4XX = "HTTP_4XX"
    TLS_ERROR = "TLS_ERROR"
    REDIRECT_BLOCKED = "REDIRECT_BLOCKED"
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"
    TOO_MANY_REDIRECTS = "TOO_MANY_REDIRECTS"
    TOO_LARGE = "TOO_LARGE"
    EMPTY_BODY = "EMPTY_BODY"
    # data-level classes (raised by pipeline steps, not the HTTP client)
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    PARSER_ERROR = "PARSER_ERROR"
    COUNT_ANOMALY = "COUNT_ANOMALY"
    FILL_RATE_BELOW_FLOOR = "FILL_RATE_BELOW_FLOOR"
    PUBLICATION_MARKER_REGRESSION = "PUBLICATION_MARKER_REGRESSION"
    STALE_NO_CHANGE = "STALE_NO_CHANGE"
    SIGNAL_WITHOUT_CHANGE = "SIGNAL_WITHOUT_CHANGE"
    CONFIG_ERROR = "CONFIG_ERROR"
    CANCELLED = "CANCELLED"
    INTERNAL = "INTERNAL"


RETRYABLE: frozenset[ErrorClass] = frozenset(
    {ErrorClass.TRANSIENT_NETWORK, ErrorClass.HTTP_5XX, ErrorClass.HTTP_429, ErrorClass.EMPTY_BODY}
)


class FetchError(Exception):
    def __init__(
        self,
        error_class: ErrorClass,
        detail: str,
        *,
        http_status: int | None = None,
        retry_after_s: float | None = None,
        evidence: dict | None = None,
    ) -> None:
        super().__init__(f"{error_class}: {detail}")
        self.error_class = error_class
        self.detail = detail
        self.http_status = http_status
        self.retry_after_s = retry_after_s
        self.evidence = evidence or {}

    @property
    def retryable(self) -> bool:
        return self.error_class in RETRYABLE


class PipelineError(Exception):
    """A data-level failure inside a pipeline step (validation, parsing, publishing)."""

    def __init__(self, error_class: ErrorClass, detail: str, *, report: dict | None = None) -> None:
        super().__init__(f"{error_class}: {detail}")
        self.error_class = error_class
        self.detail = detail
        self.report = report or {}


def classify_status(status: int) -> ErrorClass:
    if status == 429:
        return ErrorClass.HTTP_429
    if status == 403:
        return ErrorClass.HTTP_403
    if status == 404 or status == 410:
        return ErrorClass.HTTP_404
    if 500 <= status <= 599:
        return ErrorClass.HTTP_5XX
    return ErrorClass.HTTP_4XX
