"""Bounded, timed delivery; persistence is independent of the next model token."""
from contextvars import copy_context
from dataclasses import replace
import queue
import re
from threading import Event, Thread
import time

from .events import AnswerEvent, DeliveryError


class StreamRedactor:
    """Retain matching suffixes, never publish a secret split across chunks."""
    schemes = ("postgresql://", "postgres://", "mysql://")

    def __init__(self, secrets=()):
        self.secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))
        self.pending = ""
        self.url = False

    def feed(self, text):
        value = self.pending + text
        self.pending = ""
        parts, i = [], 0
        while i < len(value):
            if self.url:
                if re.match(r"[\s<>\"']", value[i]):
                    self.url = False
                else:
                    i += 1
                    continue
            suffix = value[i:]
            secret = next((s for s in self.secrets if suffix.startswith(s)), None)
            if secret:
                parts.append("[已隐藏]")
                i += len(secret)
                continue
            scheme = next((s for s in self.schemes if suffix.lower().startswith(s)), None)
            if scheme:
                parts.append("[连接配置已隐藏]")
                self.url = True
                i += len(scheme)
                continue
            if any(s.startswith(suffix) for s in self.secrets) or any(s.startswith(suffix.lower()) for s in self.schemes):
                self.pending = suffix
                break
            parts.append(value[i])
            i += 1
        return "".join(parts)

    def finish(self):
        """Close a stream without ever releasing a possible secret suffix.

        ``pending`` only contains a suffix that could be the beginning of a
        configured secret or connection URL.  At a terminal boundary it is
        safer to replace that suffix than to reveal it, even though this can
        hide an innocent word such as ``post`` at the very end of a response.
        """
        if not self.pending:
            return ""
        self.pending = ""
        return "[已隐藏]"


class BufferedDelivery:
    def __init__(self, sink, *, secrets=(), check=lambda: None, interval=.05, max_bytes=8192):
        self.sink, self.secrets, self.check = sink, secrets, check
        self.interval, self.max_bytes = interval, max_bytes
        self.queue = queue.Queue(maxsize=64)
        self.error = None
        self.pending = None
        self.due = None
        self.redactors = {}
        self.first = set()
        self.started = False
        self.closed = False
        context = copy_context()
        self.worker = Thread(target=lambda: context.run(self._run), name="answer-delivery", daemon=True)
        self.worker.start()

    def _check(self, cancel=True):
        if self.error is not None:
            raise DeliveryError("persistent answer delivery failed") from self.error
        if cancel:
            self.check()

    def _put(self, value, cancel=True):
        while True:
            self._check(cancel)
            try:
                self.queue.put(value, timeout=.05)
                return
            except queue.Full:
                pass

    def submit(self, event):
        if self.closed:
            raise DeliveryError("delivery already closed")
        if event.type == "answer.generation_started":
            self.started = True
        if event.type == "answer.text_delta":
            text = event.data["delta"]
            for i in range(0, len(text), 4096):
                self._put(replace(event, data={**event.data, "delta": text[i:i+4096]}))
        else:
            self._put(event)

    def drain(self, *, cancel=True):
        barrier = Event()
        self._put(barrier, cancel)
        while not barrier.wait(.05):
            self._check(cancel)
        self._check(cancel)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.error is None:
                self._put(None, cancel=False)
        finally:
            self.worker.join(timeout=6)
        if self.worker.is_alive():
            raise DeliveryError("delivery worker did not stop")
        self._check(False)

    def _flush(self):
        if self.pending is not None:
            self.sink(self.pending)
            self.pending = None
        self.due = None

    def _finish_redactors(self, generation_id=None):
        """Persist redactor tails before a generation terminal event."""
        for (current_id, section_id), redactor in list(self.redactors.items()):
            if generation_id is not None and current_id != generation_id:
                continue
            text = redactor.finish()
            if text:
                self._flush()
                self.sink(AnswerEvent("answer.text_delta", current_id,
                                      {"section_id": section_id, "delta": text}))

    def _run(self):
        try:
            while True:
                timeout = max(0, self.due-time.monotonic()) if self.due is not None else None
                try:
                    item = self.queue.get(timeout=timeout)
                except queue.Empty:
                    self._flush()
                    continue
                if item is None:
                    self._finish_redactors()
                    self._flush()
                    return
                if isinstance(item, Event):
                    self._flush()
                    item.set()
                    continue
                if item.type != "answer.text_delta":
                    self._flush()
                    if item.type == "answer.generation_started":
                        self.redactors.clear()
                    if item.type == "answer.generation_finished":
                        self._finish_redactors(item.generation_id)
                    self.sink(item)
                    continue
                key = (item.generation_id, item.data["section_id"])
                redactor = self.redactors.setdefault(key, StreamRedactor(self.secrets))
                text = redactor.feed(item.data["delta"])
                if not text:
                    continue
                value = replace(item, data={**item.data, "delta": text})
                if key not in self.first:
                    self._flush()
                    self.sink(value)
                    self.first.add(key)
                    continue
                if self.pending and (self.pending.generation_id, self.pending.data["section_id"]) != key:
                    self._flush()
                if self.pending:
                    value = replace(value, data={**value.data, "delta": self.pending.data["delta"]+text})
                self.pending = value
                if self.due is None:
                    self.due = time.monotonic()+self.interval
                if len(value.data["delta"].encode("utf-8")) >= self.max_bytes or time.monotonic() >= self.due:
                    self._flush()
        except BaseException as exc:
            self.error = exc
