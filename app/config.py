from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


ProviderName = Literal["openai", "deepseek", "doubao", "qwen", "codex_cli"]
AGENT_ROLES = ("product", "reader", "planner", "architecture", "coder", "reviewer", "acceptance")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AUTOFLOW_",
        extra="ignore",
    )

    app_name: str = "AutoFlow"
    provider: ProviderName = "openai"
    model: str = "gpt-5.6"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_coding_model: str = "deepseek-v4-pro"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_max_tokens: int = 32_768
    doubao_model: str = ""
    doubao_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_max_tokens: int = 16_384
    qwen_model: str = "qwen3.7-plus"
    qwen_coding_model: str = "qwen3-coder-plus"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_max_tokens: int = 32_768
    codex_model: str = "gpt-5.6-luna"
    codex_cli_path: str = "codex"
    reasoning_effort: str = "medium"
    database_path: Path = Path("./data/autoflow.db")
    worktree_root: Path = Path("./worktrees")
    allowed_roots: Annotated[list[Path], NoDecode] = [Path.home()]
    max_fix_attempts: int = 3
    max_product_iterations: int = 3
    max_acceptance_iterations: int = 2
    product_quality_threshold: int = 85
    max_context_chars: int = 80_000
    max_download_bytes: int = 100 * 1024 * 1024
    command_timeout_seconds: int = 900
    runner_token: str | None = None
    runner_offline_seconds: int = 30
    mock_llm: bool = False
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "AUTOFLOW_OPENAI_API_KEY"),
    )
    deepseek_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "AUTOFLOW_DEEPSEEK_API_KEY"),
    )
    doubao_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ARK_API_KEY", "DOUBAO_API_KEY", "AUTOFLOW_DOUBAO_API_KEY"
        ),
    )
    qwen_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DASHSCOPE_API_KEY", "QWEN_API_KEY", "AUTOFLOW_QWEN_API_KEY"
        ),
    )
    agent_product_provider: ProviderName | None = None
    agent_product_model: str | None = None
    agent_reader_provider: ProviderName | None = None
    agent_reader_model: str | None = None
    agent_planner_provider: ProviderName | None = None
    agent_planner_model: str | None = None
    agent_architecture_provider: ProviderName | None = None
    agent_architecture_model: str | None = None
    agent_coder_provider: ProviderName | None = None
    agent_coder_model: str | None = None
    agent_reviewer_provider: ProviderName | None = None
    agent_reviewer_model: str | None = None
    agent_acceptance_provider: ProviderName | None = None
    agent_acceptance_model: str | None = None

    @field_validator("allowed_roots", mode="before")
    @classmethod
    def split_roots(cls, value: object) -> object:
        if isinstance(value, str):
            return [Path(item.strip()) for item in value.split(",") if item.strip()]
        return value

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    @property
    def active_model(self) -> str:
        if self.provider == "codex_cli":
            return self.codex_model
        if self.provider == "deepseek":
            return self.deepseek_model
        if self.provider == "doubao":
            return self.doubao_model
        if self.provider == "qwen":
            return self.qwen_model
        return self.model

    def provider_for(self, role: str) -> ProviderName:
        configured = getattr(self, f"agent_{role}_provider", None)
        return configured or self.provider

    def model_for(self, role: str, fallback: str | None = None) -> str:
        configured = getattr(self, f"agent_{role}_model", None)
        if configured:
            return configured
        provider = self.provider_for(role)
        if provider == "deepseek":
            return self.deepseek_coding_model if role == "coder" else self.deepseek_model
        if provider == "doubao":
            return self.doubao_model
        if provider == "qwen":
            return self.qwen_coding_model if role == "coder" else self.qwen_model
        if provider == "codex_cli":
            return self.codex_model
        return fallback or self.model

    @property
    def agent_routes(self) -> dict[str, dict[str, str]]:
        return {
            role: {"provider": self.provider_for(role), "model": self.model_for(role)}
            for role in AGENT_ROLES
        }

    def route_errors(self) -> list[str]:
        errors: list[str] = []
        for role, route in self.agent_routes.items():
            provider = route["provider"]
            if provider == "openai" and not self.openai_api_key:
                errors.append(f"{role}: OPENAI_API_KEY 未配置")
            elif provider == "deepseek" and not self.deepseek_api_key:
                errors.append(f"{role}: DEEPSEEK_API_KEY 未配置")
            elif provider == "doubao":
                if not self.doubao_api_key:
                    errors.append(f"{role}: ARK_API_KEY/DOUBAO_API_KEY 未配置")
                if not route["model"]:
                    errors.append(f"{role}: 豆包模型或推理接入点未配置")
            elif provider == "qwen":
                if not self.qwen_api_key:
                    errors.append(f"{role}: QWEN_API_KEY/DASHSCOPE_API_KEY 未配置")
                if not route["model"]:
                    errors.append(f"{role}: 千问模型未配置")
        return errors


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
