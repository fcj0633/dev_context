import time
from dataclasses import replace

import psycopg
from openai import APIConnectionError, APIStatusError

from devcontext.code_graph.models import AnalysisContractError
from devcontext.deadline import RequestDeadlineExceeded, remaining_seconds
from devcontext.llm.errors import LLMRequestError
from devcontext.embedding.client import EmbeddingQueryError
from devcontext.tool_agent.models import ToolError, ToolResult


class ToolExecutor:
    def __init__(self, registry):
        self.registry = registry

    def execute(self, call, requirement, workspace, step, confirmed_keys):
        started = time.perf_counter()
        result = self._validate(call, requirement, confirmed_keys)
        if result is not None:
            return replace(result, latency_ms=(time.perf_counter() - started) * 1000)
        try:
            remaining_seconds()
            result = self.registry.handlers[call.tool_name](call, requirement, workspace, step)
        except RequestDeadlineExceeded:
            result = ToolResult(call, "DEADLINE", error=ToolError("DEADLINE_EXCEEDED", "Request deadline exceeded"))
        except LLMRequestError as exc:
            if not exc.retryable:
                raise
            result = ToolResult(call, "ERROR", error=ToolError("EMBEDDING_ERROR", "Temporary model service failure", True))
        except EmbeddingQueryError as exc:
            if not exc.retryable:
                raise
            result = ToolResult(call, "ERROR", error=ToolError("EMBEDDING_ERROR", "Embedding query temporarily failed", True))
        except psycopg.errors.QueryCanceled:
            result = ToolResult(call, "ERROR", error=ToolError("TOOL_TIMEOUT", "Database query timed out", True))
        except psycopg.Error:
            result = ToolResult(call, "ERROR", error=ToolError("DATABASE_ERROR", "Database query unavailable", True))
        except (TimeoutError, ConnectionError, APIConnectionError):
            result = ToolResult(call, "ERROR", error=ToolError("TOOL_TIMEOUT", "Tool transport timed out or disconnected", True))
        except APIStatusError as exc:
            if exc.status_code not in {408, 429} and exc.status_code < 500:
                raise
            result = ToolResult(call, "ERROR", error=ToolError("EMBEDDING_ERROR", "Embedding service temporarily unavailable", True))
        # Contract violations and programming errors intentionally propagate.
        if not isinstance(result, ToolResult) or result.call != call or result.status not in {
            "SUCCESS", "EMPTY", "PARTIAL", "AMBIGUOUS", "INVALID_ARGUMENT", "ERROR", "DEADLINE"}:
            raise AnalysisContractError("Tool handler returned an invalid result")
        if any(r.source_type != ("DOCUMENT" if call.tool_name == "search_docs" else "CODE") for r in result.evidence_results):
            raise AnalysisContractError("Tool returned evidence outside its source contract")
        return replace(result, latency_ms=(time.perf_counter() - started) * 1000)

    def _validate(self, call, requirement, confirmed_keys):
        spec = self.registry.specs.get(call.tool_name)
        code, message = None, None
        if spec is None:
            code, message = "INVALID_ARGUMENT", "Unknown tool"
        elif requirement is None or requirement.id != call.requirement_id:
            code, message = "INVALID_ARGUMENT", "Unknown requirement"
        elif requirement.source_requirement not in spec.allowed_sources:
            code, message = "INVALID_TOOL_FOR_REQUIREMENT", "Tool is not allowed for this evidence source"
        elif not isinstance(call.arguments, dict) or set(call.arguments) != {spec.argument}:
            code, message = "INVALID_ARGUMENT", "Unexpected or missing tool arguments"
        else:
            value = call.arguments[spec.argument]
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                code, message = "INVALID_ARGUMENT", "Argument must be nonempty and at most 1000 characters"
            elif spec.argument == "symbol_key" and value not in confirmed_keys:
                code, message = "INVALID_ARGUMENT", "Graph requires a CONFIRMED symbol exposed in the current context"
        if code:
            return ToolResult(call, "INVALID_ARGUMENT", error=ToolError(code, message))
        return None
