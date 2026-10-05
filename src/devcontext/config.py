from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from devcontext.context.budget import ModelCapabilities
from devcontext.answer_policy import RequestPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    devcontext_code_root: Path = Path(r"D:\Java-learning\12306Project\12306\my12306")
    devcontext_doc_root: Path = Path(r"D:\Java-learning\12306Project\docs")
    database_url: str = "postgresql://devcontext:devcontext_dev@localhost:5432/devcontext"
    dashscope_api_key: SecretStr | None = Field(default=None, alias="DASHSCOPE_API_KEY")
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    embedding_transport: str = "curl"
    llm_provider: Literal["deepseek", "openai"] = "deepseek"
    chatgpt_api_key: SecretStr | None = Field(default=None, alias="CHATGPT_API_KEY")
    openai_base_url: str | None = None
    openai_model: str = "gpt-6.1-sol"
    openai_v3_reasoning_effort: Literal["low", "medium", "high"] = "low"
    openai_context_window: int = Field(default=131_072, gt=0)
    openai_max_output_tokens: int = Field(default=32_768, gt=0)
    answer_profile: Literal["fast", "full"] = "fast"
    answer_reasoning_effort: Literal["low", "medium", "high"] | None = None
    answer_hard_timeout_seconds: float | None = Field(default=None, gt=0)
    answer_fast_latency_target_seconds: float = Field(default=45, gt=0)
    answer_full_latency_target_seconds: float = Field(default=300, gt=0)
    answer_planner_timeout_seconds: float = Field(default=180, gt=0)
    answer_writer_timeout_seconds: float = Field(default=240, gt=0)
    answer_full_planner_timeout_seconds: float = Field(default=300, gt=0)
    answer_full_writer_timeout_seconds: float = Field(default=600, gt=0)
    # Explicit capability override for compatible gateways; unknown means omit.
    llm_supported_reasoning_efforts: str | None = None
    answer_engine_enabled: bool = False
    answer_request_policy: RequestPolicy | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def supplement_windows_environment(cls, values):
        # BaseSettings has already merged process environment and project .env.
        # Only fill missing credentials/address; never override explicit values.
        import os
        if os.name != "nt" or not isinstance(values, dict):
            return values
        import winreg
        values = dict(values)
        present = {str(key).lower() for key in values}
        for field, name in (("chatgpt_api_key", "CHATGPT_API_KEY"),
                            ("openai_base_url", "OPENAI_BASE_URL")):
            if field.lower() in present or name.lower() in present:
                continue
            for root, path in ((winreg.HKEY_CURRENT_USER, "Environment"),
                               (winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(root, path) as registry:
                        value = winreg.QueryValueEx(registry, name)[0]
                    if isinstance(value, str) and value.strip():
                        values[name if field == "chatgpt_api_key" else field] = value.strip()
                        break
                except OSError:
                    pass
        return values

    def text_model(self, legacy_model: str | None = None) -> str:
        return self.openai_model if self.llm_provider == "openai" else (legacy_model or self.deepseek_model)

    def text_key(self) -> str:
        if self.llm_provider == "deepseek":
            return self.deepseek_key()
        if self.chatgpt_api_key is None:
            raise ValueError("CHATGPT_API_KEY is not configured")
        return self.chatgpt_api_key.get_secret_value()

    def text_base_url(self) -> str:
        if self.llm_provider == "deepseek":
            return self.deepseek_base_url
        if not self.openai_base_url or not self.openai_base_url.strip():
            raise ValueError("OPENAI_BASE_URL is not configured")
        return self.openai_base_url

    deepseek_api_key: SecretStr | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_planner_model: str | None = None
    deepseek_answer_planner_model: str | None = None
    deepseek_answer_model: str | None = None
    deepseek_reviewer_model: str | None = None
    teaching_request_timeout_seconds: int = Field(default=180, ge=60, le=900)
    teaching_generation_mode: Literal["multi_pass", "single_stream", "v3"] = "multi_pass"
    source_policy_path: Path | None = None
    repository_name: str = "my12306"
    # Effective ceilings for this pipeline, not the model's advertised limits.
    # deepseek-flash documents a 1M window and 384K max output, but the writers
    # here cap their own output well below that, so budgeting against 384K would
    # promise room the pipeline cannot use. Raise these together with the writer
    # token caps, not before them.
    deepseek_context_window: int = 131_072
    deepseek_max_output_tokens: int = 32_768

    def model_capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            context_window=self.openai_context_window if self.llm_provider == "openai" else self.deepseek_context_window,
            max_output_tokens=self.openai_max_output_tokens if self.llm_provider == "openai" else self.deepseek_max_output_tokens,
        )

    def validate_sources(self) -> None:
        missing = [
            str(path)
            for path in (self.devcontext_code_root, self.devcontext_doc_root)
            if not path.is_dir()
        ]
        if missing:
            raise ValueError(f"Source directories do not exist: {', '.join(missing)}")
        if self.embedding_transport not in {"auto", "openai", "curl"}:
            raise ValueError("EMBEDDING_TRANSPORT must be auto, openai, or curl")

    def api_key(self) -> str:
        if self.dashscope_api_key is None:
            raise ValueError("DASHSCOPE_API_KEY is not configured")
        return self.dashscope_api_key.get_secret_value()

    def deepseek_key(self) -> str:
        if self.deepseek_api_key is None:
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        return self.deepseek_api_key.get_secret_value()


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
