"""Backward-compatible DeepSeek facade over the shared curl transport."""
import shutil
import subprocess
from devcontext.llm.chat_completions import ChatCompletionsLLMClient

class DeepSeekLLMClient(ChatCompletionsLLMClient):
    pass
