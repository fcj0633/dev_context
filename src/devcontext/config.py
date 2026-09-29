from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    deepseek_api_key: SecretStr | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_planner_model: str | None = None
    deepseek_answer_planner_model: str | None = None
    deepseek_answer_model: str | None = None
    deepseek_reviewer_model: str | None = None
    source_policy_path: Path | None = None
    repository_name: str = "my12306"

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
