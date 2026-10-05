import pytest
from devcontext.llm.openai_compatible import ReasoningPrefixFilter, OpenAICompatibleLLMClient
from devcontext.llm.chat_completions import ChatCompletionsLLMClient
from devcontext.explanation.v3.prompts import planner_prompt


@pytest.mark.parametrize("size", [1, 2, 7, 19, 1000])
def test_gateway_reasoning_prefix_across_chunk_boundaries(size):
    text = '  <think>internal reasoning</think>\n{"schema_version":2}'
    prefix = ReasoningPrefixFilter()
    output = ''.join(prefix.feed(text[i:i+size]) for i in range(0, len(text), size)) + prefix.finish()
    assert output.strip() == '{"schema_version":2}'


def test_literal_tag_in_answer_is_preserved():
    prefix = ReasoningPrefixFilter()
    text = '<<<SECTION:S1>>> example <think>literal</think>'
    assert prefix.feed(text) + prefix.finish() == text


def test_unfinished_reasoning_is_not_a_success():
    prefix = ReasoningPrefixFilter()
    assert prefix.feed('<think>unfinished') == ''
    with pytest.raises(RuntimeError, match='incomplete reasoning'):
        prefix.finish()


def test_gateway_json_is_normalized_before_planner(monkeypatch):
    monkeypatch.setattr(ChatCompletionsLLMClient, '_generate',
                        lambda self, messages: '<think>reason</think> {"status":"OK"}')
    client = OpenAICompatibleLLMClient('test', 'https://example.invalid/v1')
    assert client.generate([]) == '{"status":"OK"}'


def test_demo_selection_keeps_both_schemas_and_correct_complete_example():
    why = planner_prompt("项目为何使用责任链？")
    how = planner_prompt("如何发布内容？")
    assert "WHY:" in why and "HOW:" in how
    assert "规则能执行" not in how
    assert "完整虚构蓝图示范" in why and "完整虚构蓝图示范" in how
    assert "selected_depth" not in why + how


def test_openai_v3_effort_is_configurable_without_changing_other_modes():
    from devcontext.config import Settings
    from devcontext.cli import _v3_client_factory, _teaching_writer_factory
    settings = Settings(_env_file=None, llm_provider='openai', CHATGPT_API_KEY='test',
                        openai_base_url='https://example.invalid/v1')
    factory = _teaching_writer_factory(settings)
    assert factory().reasoning_effort == 'high'
    assert _v3_client_factory(settings, factory)().reasoning_effort == 'low'
    settings.openai_v3_reasoning_effort = 'high'
    assert _v3_client_factory(settings, factory)().reasoning_effort == 'high'
