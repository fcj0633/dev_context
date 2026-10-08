"""Public configuration fingerprints; credentials are never serialized."""
from hashlib import sha256
import json


def configuration_hash(settings):
    names = ('llm_provider', 'openai_model', 'openai_context_window',
             'deepseek_model', 'deepseek_planner_model', 'deepseek_context_window',
             'embedding_model', 'tool_agent_planner_timeout_seconds',
             'symbol_graph_query_timeout_seconds', 'answer_hard_timeout_seconds')
    from devcontext.planning.evidence_planner import EVIDENCE_PLANNER_SYSTEM_PROMPT
    from devcontext.agentic.fast import FAST_RETRIEVAL_PROMPT
    from devcontext.tool_agent.planner import SYSTEM_PROMPT
    from devcontext.agentic.coverage import COVERAGE_SYSTEM_PROMPT
    value = {name: getattr(settings, name, None) for name in names}
    value['planning_prompts'] = [EVIDENCE_PLANNER_SYSTEM_PROMPT, FAST_RETRIEVAL_PROMPT, SYSTEM_PROMPT, COVERAGE_SYSTEM_PROMPT]
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
