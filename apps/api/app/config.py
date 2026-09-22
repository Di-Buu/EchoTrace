from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_origin: str = "http://localhost:8081"
    supabase_url: str = ""
    supabase_anon_key: str = ""
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_model: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 1024
    auto_insight_min_memories: int = 4
    ai_timeout_seconds: float = Field(default=45.0, ge=5, le=180)
    insight_execution_profile: Literal["local_quality", "online_demo"] = "local_quality"
    insight_ai_timeout_seconds: float = Field(default=120.0, ge=10, le=300)
    insight_enable_thinking: bool = True
    vercel: bool = False

    @property
    def use_quality_insight_reasoning(self) -> bool:
        return (
            not self.vercel
            and self.insight_execution_profile == "local_quality"
            and self.insight_enable_thinking
        )

    @property
    def supabase_rest_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/rest/v1"

    @property
    def supabase_auth_user_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/user"


@lru_cache
def get_settings() -> Settings:
    return Settings()
