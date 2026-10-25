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
    weekly_insight_min_total_moments: int = Field(default=8, ge=2, le=100)
    weekly_insight_min_new_moments: int = Field(default=2, ge=1, le=50)
    ai_timeout_seconds: float = Field(default=45.0, ge=5, le=180)
    insight_execution_profile: Literal["local_quality", "online_demo"] = "local_quality"
    insight_ai_timeout_seconds: float = Field(default=120.0, ge=10, le=300)
    insight_enable_thinking: bool = True
    vercel: bool = False
    supabase_service_role_key: str = ""
    worker_poll_seconds: float = Field(default=2, ge=0.1, le=60)
    task_timeout_seconds: int = Field(default=600, ge=30, le=1800)
    task_lease_seconds: int = Field(default=45, ge=15, le=300)
    agent_max_steps: int = Field(default=5, ge=1, le=10)
    agent_max_calls: int = Field(default=24, ge=2, le=60)
    agent_max_tokens: int = Field(default=80000, ge=1000, le=200000)
    retrieval_fusion: Literal["rrf", "legacy"] = "rrf"
    analysis_strategy: Literal["dynamic", "fixed"] = "dynamic"
    agent_max_parallel: int = Field(default=2, ge=1, le=4)
    agent_max_input_characters: int = Field(default=50000, ge=4000, le=100000)

    @property
    def use_quality_insight_reasoning(self) -> bool:
        return not self.vercel and self.insight_execution_profile == "local_quality" and self.insight_enable_thinking

    @property
    def supabase_rest_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/rest/v1"

    @property
    def supabase_auth_user_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/user"


@lru_cache
def get_settings() -> Settings:
    return Settings()
