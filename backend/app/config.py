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

    # 上下文预算（token）。P0 材料只有几百 token，默认值远大于实际用量；
    # 演示/测试时可用 CONTEXT_TOKEN_BUDGET 调小，观察裁剪行为。
    context_token_budget: int = 2000

    # ---- LLM 成本控制（单次运行）----
    # 超过任一上限 → 不调用模型 / 标记 stop_reason=budget_exceeded
    max_tokens_per_run: int = 20000
    max_cost_cny_per_run: float = 0.05
    # 价格按当期官方定价填（这里只是量级参考）
    llm_price_in_cny_per_mtok: float = 2.0
    llm_price_out_cny_per_mtok: float = 8.0

    # LangGraph checkpointer 后端：auto（prod→postgres，其它→memory）/ memory / postgres。
    # 审核挂起状态要靠它活过进程重启，所以生产必须用 postgres。
    checkpointer_backend: str = "auto"  # 环境变量 CHECKPOINTER_BACKEND
    # 留空则从 database_url 推导（会把 SQLAlchemy 的 "+psycopg" 前缀去掉）
    checkpointer_dsn: str = ""
    # 连接池上限：checkpointer 的读写都走这个池，别和应用自身的 SQLAlchemy 池抢满连接
    checkpointer_pool_size: int = 5

    # ---- 麦当劳 MCP（mcp.mcd.cn）：官方营养库，规划后额外调一次，可选 ----
    # 地址是根路径（官方文档：Streamable HTTP，加 /mcp 会 404）；
    # token 留空 = 不启用：工具不会暴露给模型，规划流程照常
    mcd_mcp_url: str = "https://mcp.mcd.cn"
    mcd_mcp_token: str = ""
    mcd_mcp_tool: str = "list-nutrition-foods"
    mcd_mcp_timeout: float = 15.0
    # 是否允许"帮你点好（到付款前）"：建单是真的在麦当劳侧生成待支付订单，
    # 想关掉写操作就设 MCD_MCP_ALLOW_ORDER=0（读营养库不受影响）
    mcd_mcp_allow_order: bool = True

    # ---- 注册 / 登录防滥用 ----
    # 账号总量硬天花板：到顶就拒绝注册，避免被刷爆磁盘
    max_users: int = 5000
    # 单 IP 每小时 / 每天最多成功注册几次
    register_per_ip_hour: int = 5
    register_per_ip_day: int = 15
    # 同一手机号每天最多提交几次注册（防占号/重放）
    register_per_phone_day: int = 3
    # 尝试频率（包含失败请求）：只防刷接口，不惩罚"密码填错/点太快"
    register_try_per_ip_hour: int = 20
    register_try_per_ip_day: int = 60
    # 全站注册熔断：一小时内最多注册多少个账号
    register_global_hour: int = 200
    # 登录失败阈值（同 IP + 同手机号，窗口内）
    login_fail_per_window: int = 8
    login_window_minutes: int = 15
    # 触发阈值后锁多久（返回 429 + Retry-After）
    login_lock_minutes: int = 15
    # 一次性校验票据：有效期与"最短填表耗时"
    auth_challenge_ttl_seconds: int = 600
    auth_min_fill_ms: int = 1200
    # 部署在 Nginx 后面时才会信任 X-Forwarded-For
    trust_proxy: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 白名单切成列表，直接喂给 CORSMiddleware。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程内单例：.env 只解析一次；测试可绕过它自行构造 Settings。"""
    return Settings()
