from dataclasses import dataclass, asdict
from uuid import uuid4


class DeliveryError(BaseException):
    """Delivery failure must escape model retry/fallback handlers."""


@dataclass(frozen=True, slots=True)
class AnswerEvent:
    type: str
    generation_id: str
    data: dict

    def to_dict(self):
        return asdict(self)


class Generation:
    def __init__(self, path):
        self.id = str(uuid4())
        self.emit("answer.generation_started", path=path)

    def emit(self, kind, **data):
        from devcontext.execution import publish_answer
        publish_answer(AnswerEvent(kind, self.id, data))

    def parser_update(self, kind, section_id, text=""):
        if kind == "start":
            self.emit("answer.section_started", section_id=section_id)
        elif kind == "delta" and text:
            self.emit("answer.text_delta", section_id=section_id, delta=text)

    def commit(self, section):
        self.emit("answer.section_upsert", section_id=section.section_id,
                  markdown=section.markdown, citations=list(section.citations))

    def finish(self, status):
        self.emit("answer.generation_finished", status=status)
