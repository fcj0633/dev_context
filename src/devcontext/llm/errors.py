"""Transport failures with explicit recovery policy, shared by both providers."""
from contextlib import contextmanager
from contextvars import ContextVar

_request_failure = ContextVar("full_request_failure", default=None)


@contextmanager
def fatal_request_scope():
    token = _request_failure.set([])
    try:
        yield
    finally:
        _request_failure.reset(token)


def check_fatal_request():
    failures = _request_failure.get()
    if failures:
        raise failures[0]


class LLMRequestError(RuntimeError):
    def __init__(self, message, *, retryable=False, status=None, category="configuration"):
        super().__init__(message)
        self.retryable = retryable
        self.recoverable = retryable
        self.status = status
        self.category = category
        failures = _request_failure.get()
        if failures is not None and not retryable and not failures:
            failures.append(self)


def http_failure(message, status):
    retryable = status in {0, 408, 429} or status >= 500
    return LLMRequestError(message, status=status, retryable=retryable,
                           category="transient" if retryable else "configuration")


def recoverable(exc):
    from devcontext.deadline import RequestDeadlineExceeded
    if isinstance(exc, RequestDeadlineExceeded):
        return False
    return getattr(exc, "recoverable", getattr(exc, "retryable", False))
