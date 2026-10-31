from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "PlanHarness API"
    env: str = "dev"
    database_url: str = "sqlite:///./planharness.db"
    secret_key: str = "dev-only-change-me"
    access_token_minutes: int = 60 * 24 * 7
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "https://www.agentworkout.online"
    )

    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_s: float = 30.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
