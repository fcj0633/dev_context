"""A request-local deadline shared by retrieval and generation transports."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline: ContextVar[float | None] = ContextVar("request_deadline", default=None)


class RequestDeadlineExceeded(TimeoutError):
    pass


def remaining_seconds() -> float | None:
    value = _deadline.get()
    if value is None:
        return None
    remaining = value - time.perf_counter()
    if remaining <= 0:
        raise RequestDeadlineExceeded("请求已达到显式总时间预算")
    return remaining


def bounded_timeout(default: float) -> float:
    remaining = remaining_seconds()
    return default if remaining is None else min(default, remaining)


@contextmanager
def request_deadline(seconds: float | None = 180, *, started: float | None = None):
    current = _deadline.get()
    expires = None if seconds is None else (time.perf_counter() if started is None else started) + seconds
    if expires is None:
        yield
        return
    token = _deadline.set(min(current, expires) if current is not None else expires)
    try:
        yield
    finally:
        _deadline.reset(token)
