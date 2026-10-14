"""Optional request-local delivery hooks. No hooks means unchanged CLI behavior."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event


class RequestCancelled(BaseException):
    """Control flow, deliberately outside algorithm retry/fallback handlers."""


class CancellationToken:
    def __init__(self):
        self.event = Event()

    def cancel(self):
        self.event.set()

    def check(self):
        if self.event.is_set():
            raise RequestCancelled()


_token = ContextVar("execution_cancellation", default=None)
_evidence = ContextVar("execution_evidence", default=None)
_answer = ContextVar("execution_answer", default=None)


def check_cancelled():
    token = _token.get()
    if token is not None:
        token.check()


def publish_evidence(bundle):
    check_cancelled()
    callback = _evidence.get()
    if callback is not None:
        callback(bundle)


def publish_answer(event):
    callback = _answer.get()
    if callback is None:
        return
    check_cancelled()
    from devcontext.delivery.events import DeliveryError
    try:
        callback(event)
    except Exception as exc:
        raise DeliveryError("answer delivery failed") from exc


def answer_subscribed():
    return _answer.get() is not None


@contextmanager
def execution_scope(token=None, on_evidence=None, on_event=None):
    t = _token.set(token)
    e = _evidence.set(on_evidence)
    a = _answer.set(on_event)
    try:
        check_cancelled()
        yield
    finally:
        _answer.reset(a)
        _evidence.reset(e)
        _token.reset(t)
