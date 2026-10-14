"""NDJSON adapter, with the same cross-chunk disclosure boundary as the Web."""
import json
import sys
from uuid import uuid4

from devcontext.observability.diagnostics import clean
from .buffer import StreamRedactor


class EventPrinter:
    def __init__(self, settings):
        self.secrets = tuple(v.get_secret_value() for v in vars(settings).values() if hasattr(v, "get_secret_value")) + (settings.database_url,)
        self.id, self.seq = str(uuid4()), 0
        self.redactors = {}

    def emit(self, kind, data):
        self.seq += 1
        print(json.dumps({"run_id": self.id, "seq": self.seq, "type": kind,
                          "data": clean(data, self.secrets)}, ensure_ascii=False), flush=True)

    def __call__(self, event):
        data = {**event.data, "generation_id": event.generation_id}
        if event.type == "answer.generation_started":
            self.redactors.clear()
        if event.type == "answer.generation_finished":
            for (generation_id, section_id), redactor in list(self.redactors.items()):
                if generation_id != event.generation_id:
                    continue
                tail = redactor.finish()
                if tail:
                    self.emit("answer.text_delta", {"generation_id": generation_id,
                                                     "section_id": section_id,
                                                     "delta": tail})
        if event.type == "answer.text_delta":
            key = (event.generation_id, data["section_id"])
            data["delta"] = self.redactors.setdefault(key, StreamRedactor(self.secrets)).feed(data["delta"])
            if not data["delta"]:
                return
        self.emit(event.type, data)
