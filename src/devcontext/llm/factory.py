from devcontext.config import Settings
from devcontext.llm.deepseek import DeepSeekLLMClient
from devcontext.llm.openai_compatible import OpenAICompatibleLLMClient


def create_llm_client(settings: Settings, *, legacy_model=None, model=None,
                      deepseek_client_type=None, **kwargs):
    client_type = (OpenAICompatibleLLMClient if settings.llm_provider == "openai"
                   else (deepseek_client_type or DeepSeekLLMClient))
    return client_type(api_key=settings.text_key(), base_url=settings.text_base_url(),
                       model=model or settings.text_model(legacy_model), **kwargs)
