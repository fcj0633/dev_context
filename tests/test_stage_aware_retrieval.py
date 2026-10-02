from __future__ import annotations

import pytest

from devcontext.agentic.retrieval_controller import RetrievalController
from devcontext.agentic.evidence_models import SearchAction
from devcontext.context import EvidenceWorkspace
from devcontext.evidence import EvidenceCandidate, SourcePolicy
from devcontext.models import SearchExecution, SearchResult, SearchTimings
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import UserRequest
from devcontext.retrieval.policy import RetrievalPolicy
from devcontext.retrieval.symbols import (
    code_strategy_for,
    detected_symbols,
    symbols_in_text,
    workspace_symbols,
)


# --- symbol detection --------------------------------------------------------


class TestSymbolDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "购票占座 请求处理链路 入口方法 核心服务方法 调用顺序 数据写入",
            "订单创建 幂等 唯一约束 幂等键 重复请求拦截 去重表 状态机",
            "Redis 锁 补偿 事务 订单",
            "两个请求同时购买同一张票时，系统如何避免重复售卖",
        ],
    )
    def test_domain_vocabulary_is_not_a_symbol(self, text: str) -> None:
        """The plan is explicit: 订单/事务/Redis/锁/补偿 must not count."""
        assert symbols_in_text(text) == ()

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("loadBucket 在哪些地方被调用", "loadBucket"),
            ("closeTimeoutOrder 的实现", "closeTimeoutOrder"),
            ("PurchaseTicketTxService 座位状态 条件更新", "PurchaseTicketTxService"),
            ("@Transactional 注解加在哪里", "@Transactional"),
            ("UserReuseUtil.java 里做了什么", "UserReuseUtil.java"),
        ],
    )
    def test_java_identifiers_are_detected(self, text: str, expected: str) -> None:
        assert expected in symbols_in_text(text)

    def test_lower_camel_case_is_detected(self) -> None:
        """The router's own pattern requires a leading capital and misses these."""
        assert symbols_in_text("doPurchaseInTransaction 做了什么") == (
            "doPurchaseInTransaction",
        )

    def test_acronyms_are_not_symbols(self) -> None:
        assert symbols_in_text("HTTP 状态码怎么处理") == ()
        assert symbols_in_text("SQL 注入怎么防") == ()

    def test_strategy_follows_symbol_presence_not_round(self) -> None:
        strategy, symbols = code_strategy_for("购票 库存 扣减 并发控制", None, "ER1")
        assert (strategy, symbols) == ("vector", ())

        strategy, symbols = code_strategy_for("takeToken 的实现", None, "ER1")
        assert strategy == "keyword"
        assert "takeToken" in symbols


# --- evidence-confirmed symbols ---------------------------------------------


def _workspace_with(*, class_name: str | None, requirement_id: str = "ER1"):
    workspace = EvidenceWorkspace("问题")
    workspace.ingest(
        [
            EvidenceCandidate(
                requirement_id,
                SearchResult(
                    1, "CODE", "METHOD", "PurchaseTicketTxService.java", "body",
                    1, 2, class_name, "doPurchaseInTransaction", "void doPurchase()",
                    title=None, score=1.0,
                ),
                "IMPLEMENTATION", "CURRENT", 100,
            )
        ],
        0,
    )
    return workspace


class TestWorkspaceSymbols:
    def test_citation_names_are_confirmed_symbols(self) -> None:
        workspace = _workspace_with(class_name="PurchaseTicketTxService")

        symbols = workspace_symbols(workspace, "ER1")

        assert "PurchaseTicketTxService" in symbols
        assert "doPurchaseInTransaction" in symbols
        assert "void doPurchase()" in symbols

    def test_a_file_path_is_not_a_symbol(self) -> None:
        """It says where a thing lives, not what it is called."""
        workspace = _workspace_with(class_name="PurchaseTicketTxService")

        assert "PurchaseTicketTxService.java" not in workspace_symbols(workspace, "ER1")

    def test_round_one_finds_symbols_the_query_never_named(self) -> None:
        """This is the round-1 rule: evidence, not the question, supplies it."""
        workspace = _workspace_with(class_name="PurchaseTicketTxService")

        symbols = detected_symbols("座位状态 条件更新", workspace, "ER1")

        assert "PurchaseTicketTxService" in symbols

    def test_no_workspace_means_no_evidence_symbols(self) -> None:
        assert detected_symbols("座位状态 条件更新", None, "ER1") == ()


# --- the policy seam ---------------------------------------------------------


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, str | None]] = []

    def search_with_trace(
        self, strategy: str, query: str, top_k: int = 10, *, source_type=None
    ) -> SearchExecution:
        self.calls.append((strategy, query, top_k, source_type))
        return SearchExecution(
            [SearchResult(1, source_type or "CODE", "METHOD", "S.java", "b", 1, 2,
                          "S", "m", None, title=None, score=1.0)],
            SearchTimings(),
        )


class TestPolicySeam:
    def test_without_a_code_strategy_the_default_is_unchanged(self) -> None:
        service = RecordingService()

        RetrievalPolicy(service).search_scope_with_trace("查询", "CODE", 5)

        assert [call[0] for call in service.calls] == ["hybrid"]

    def test_an_explicit_strategy_narrows_the_code_case_only(self) -> None:
        service = RecordingService()
        policy = RetrievalPolicy(service)

        policy.search_scope_with_trace("查询", "CODE", 5, code_strategy="vector")
        policy.search_scope_with_trace("查询", "DOCUMENT", 5, code_strategy="vector")

        assert [(call[0], call[3]) for call in service.calls] == [
            ("vector", "CODE"),
            ("vector", "DOCUMENT"),
        ]

    def test_an_unknown_strategy_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            RetrievalPolicy(RecordingService()).search_scope_with_trace(
                "查询", "CODE", 5, code_strategy="hybrid"
            )


# --- the controller's choice -------------------------------------------------


class _FakePolicy:
    def __init__(self) -> None:
        self.strategies: list[str | None] = []

    def search_scope_with_trace(
        self, query: str, scope: str, top_k: int, *, code_strategy: str | None = None
    ) -> SearchExecution:
        self.strategies.append(code_strategy)
        return SearchExecution(
            [SearchResult(1, "CODE", "METHOD", "S.java", "b", 1, 2, "S", "m",
                          None, title=None, score=1.0)],
            SearchTimings(),
        )


class _Plan:
    last_client = None

    def plan(self, query: str) -> EvidencePlan:
        return EvidencePlan(
            query,
            (EvidenceRequirement("ER1", "t", "c", "CORE", "CURRENT", "CODE"),),
            "fallback",
        )


class _Actions:
    last_client = None

    def plan_actions(self, query, requirements, *, round_index, **_):
        return (
            SearchAction(
                f"SA-{round_index}", "ER1", round_index,
                "购票 库存 扣减 并发控制" if round_index == 0 else "座位状态 条件更新",
                "CODE", "test", "fallback",
            ),
        )


class _Coverage:
    last_client = None

    def check(self, requirements, view):
        from devcontext.agentic.evidence_models import RequirementCoverage

        return tuple(
            RequirementCoverage(r.id, "PARTIAL", (), ("gap",), "reason", "rules")
            for r in requirements
        )


class TestControllerChoosesByStage:
    def test_round_zero_semantic_query_searches_by_vector(self) -> None:
        policy = _FakePolicy()
        controller = RetrievalController(
            _Plan(), _Actions(), policy, _Coverage(), SourcePolicy()
        )

        controller.retrieve(UserRequest("购票占座是怎么保证一致的"), 10)

        # Round 0 has nothing but business semantics, so no exact term exists.
        assert policy.strategies[0] == "vector"

    def test_round_one_uses_keyword_once_evidence_names_a_symbol(self) -> None:
        policy = _FakePolicy()
        controller = RetrievalController(
            _Plan(), _Actions(), policy, _Coverage(), SourcePolicy()
        )
        original = controller.retrieve

        # Round 0 finds a real class, so by round 1 the workspace can name one
        # even though the round-1 query itself does not.
        controller._execute = _execute_with_symbol_seeded(controller._execute)

        controller.retrieve(UserRequest("购票占座是怎么保证一致的"), 10)

        assert policy.strategies[0] == "vector"
        assert policy.strategies[1] == "keyword"


def _execute_with_symbol_seeded(original):
    """Run one real round-0 execute, then seed a symbol into the workspace."""

    def wrapper(actions, plan, pool, workspace, round_index):
        result = original(actions, plan, pool, workspace, round_index)
        if round_index == 0:
            workspace.ingest(
                [
                    EvidenceCandidate(
                        "ER1",
                        SearchResult(
                            99, "CODE", "METHOD", "PurchaseTicketTxService.java",
                            "body", 1, 2, "PurchaseTicketTxService",
                            "doPurchaseInTransaction", None,
                            title=None, score=1.0,
                        ),
                        "IMPLEMENTATION", "CURRENT", 100,
                    )
                ],
                0,
            )
        return result

    return wrapper
