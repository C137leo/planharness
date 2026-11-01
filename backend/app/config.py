"""运行配置。

配置优先级：环境变量 > backend/.env > 本文件里的默认值。
pydantic-settings 会按字段类型自动转换，所以这里只需要声明字段。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用级配置；字段名即环境变量名（大小写不敏感）。"""

    # extra="ignore"：忽略 .env 里不属于本类的键，避免部署时多塞变量导致启动失败
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "PlanHarness API"
    env: str = "dev"
    # 默认落本地 SQLite，零依赖即可跑通；生产用 DATABASE_URL 指向 Postgres
    database_url: str = "sqlite:///./planharness.db"
    # 仅开发可用的默认密钥，生产必须用环境变量覆盖
    secret_key: str = "dev-only-change-me"
    # 演示期用较长有效期（7 天），避免演示过程中掉登录
    access_token_minutes: int = 60 * 24 * 7
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "https://www.agentworkout.online"
    )

    # DeepSeek：P0 阶段 Key 只存服务器 .env，不写进代码、不经过聊天记录
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout_s: float = 30.0

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 白名单切成列表，直接喂给 CORSMiddleware。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程内单例：.env 只解析一次；测试可绕过它自行构造 Settings。"""
    return Settings()
