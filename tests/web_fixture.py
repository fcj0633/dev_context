"""Deterministic test-only workflow, run through the real FastAPI and SQLite stack."""
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time

from devcontext.config import Settings
from devcontext.explanation.stream_writer import ValidatedSection
from devcontext.web.api import create_app
from test_web_v1 import bundle

database = TemporaryDirectory(prefix="devcontext-e2e-")


class FixtureService:
    def ask(self, request, **hooks):
        token = hooks["cancellation"]
        if request.query == "fail":
            raise RuntimeError("fixture retrieval failure")
        hooks["observer"].on_action_completed(SimpleNamespace(source_scope="CODE", round_index=0),
            SimpleNamespace(strategy="hybrid", results=[1], timings=SimpleNamespace(total_ms=10)))
        hooks["on_evidence"](bundle())
        sections = []
        for number in range(1, 3):
            for _ in range(20 if request.query == "slow" else 4):
                token.check()
                time.sleep(.1)
            section = ValidatedSection(f"S{number}", f"## 实现解释 {number}\n项目订单方法来自真实证据 [E1]。\n```java\nvoid order() {{}}\n```", ("E1",))
            hooks["on_section"](section)
            sections.append(section.markdown)
        return SimpleNamespace(answer_result=SimpleNamespace(answer="\n\n".join(sections)), context_bundle=bundle(), evidence_catalog=None,
            trace=SimpleNamespace(teaching={"completion_status": "partial" if request.query == "partial" else "complete"},
            agent_retrieval={}, stage_usage=[], final_coverage=[], evidence_package_state="READY", stop_reason="ready", retry_count=0))


app = create_app(database_path=f"{database.name}/web.db", settings=Settings(_env_file=None), service=FixtureService())
