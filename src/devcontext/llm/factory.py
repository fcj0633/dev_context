from devcontext.config import Settings
from devcontext.llm.deepseek import DeepSeekLLMClient
from devcontext.llm.openai_compatible import OpenAICompatibleLLMClient


def reasoning_capabilities(settings, model):
    if settings.llm_supported_reasoning_efforts is not None:
        values = {x.strip() for x in settings.llm_supported_reasoning_efforts.split(",") if x.strip()}
        if not values <= {"low", "medium", "high"}:
            raise ValueError("LLM_SUPPORTED_REASONING_EFFORTS must contain low,medium,high")
        return values
    if settings.llm_provider == "deepseek" and model == "deepseek-flash":
        return {"low", "high"}
    # Other model names/gateways require a declared capability. Acceptance of a
    # string by an SDK is not proof of server support.
    return set()


def create_llm_client(settings: Settings, *, legacy_model=None, model=None,
                      deepseek_client_type=None, requested_reasoning_effort=None, **kwargs):
    client_type = (OpenAICompatibleLLMClient if settings.llm_provider == "openai"
                   else (deepseek_client_type or DeepSeekLLMClient))
    selected_model = model or settings.text_model(legacy_model)
    if requested_reasoning_effort is not None:
        supported = reasoning_capabilities(settings, selected_model)
        kwargs["reasoning_effort"] = requested_reasoning_effort if requested_reasoning_effort in supported else None
    client = client_type(api_key=settings.text_key(), base_url=settings.text_base_url(),
                       model=selected_model, **kwargs)
    client.requested_reasoning_effort = requested_reasoning_effort or kwargs.get("reasoning_effort", "low")
    client.reasoning_omission_reason = ("model capability does not declare requested effort support"
        if requested_reasoning_effort is not None and kwargs["reasoning_effort"] is None else None)
    return client
