"""离线可用的 API 文档页（`/docs`）。

**为什么不用 FastAPI 默认的 /docs？**
FastAPI 默认生成的页面从 `cdn.jsdelivr.net` 加载 Swagger UI 的 JS/CSS。
国内网络访问该域名经常被重置（`curl` 报 `Recv failure: Connection reset by peer`），
浏览器拿不到 JS 就只剩一张白页 —— 接口本身是好的，只是"文档壳子"加载失败。

**本模块的做法**
把 Swagger UI 的静态文件放进 `app/static/swagger/`，由本站自己提供：

    /static/swagger/swagger-ui.css
    /static/swagger/swagger-ui-bundle.js
    /static/swagger/swagger-ui-standalone-preset.js
    /static/swagger/favicon-32x32.png

于是断网、内网、面试现场 Wi-Fi 很差时都能正常打开文档并点着试接口。

静态文件版本：swagger-ui-dist 5.17.14（Apache-2.0，LICENSE/NOTICE 已随包附带）。
升级方式见 app/static/swagger/README.md。
"""

from __future__ import annotations

from pathlib import Path
from string import Template

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"
SWAGGER_DIR = STATIC_DIR / "swagger"

# 用 Template 而不是 f-string：页面里有 JS 花括号，f-string 需要到处转义，容易出错
_DOCS_PAGE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="/static/swagger/swagger-ui.css">
  <link rel="shortcut icon" href="/static/swagger/favicon-32x32.png">
  <title>$title - API 文档</title>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="/static/swagger/swagger-ui-bundle.js"></script>
  <script src="/static/swagger/swagger-ui-standalone-preset.js"></script>
  <script>
    window.ui = SwaggerUIBundle({
      url: "$openapi_url",
      dom_id: "#swagger-ui",
      layout: "StandaloneLayout",
      deepLinking: true,
      // 刷新页面后不用重新贴 token，演示时少一步操作
      persistAuthorization: true,
      displayRequestDuration: true,
      filter: true,
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset]
    });
  </script>
</body>
</html>
"""
)


def mount_api_docs(app: FastAPI, *, title: str) -> None:
    """挂载 /static 与 /docs（含 /redoc 的降级处理）。

    静态文件缺失时（例如打包漏了数据文件）回退到 FastAPI 默认的 CDN 版本，
    宁可慢一点，也不要让 /docs 直接 500。
    """
    if not (SWAGGER_DIR / "swagger-ui-bundle.js").is_file():
        @app.get("/docs", include_in_schema=False)
        def docs_from_cdn() -> HTMLResponse:
            """静态文件缺失时的兜底：退回 FastAPI 默认的 CDN 方案（可能白屏，但不 500）。"""
            return get_swagger_ui_html(
                openapi_url=app.openapi_url or "/openapi.json",
                title=f"{title} - Swagger UI",
            )

        return

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/docs", include_in_schema=False)
    def swagger_ui() -> HTMLResponse:
        return HTMLResponse(
            _DOCS_PAGE.safe_substitute(title=title, openapi_url=app.openapi_url or "/openapi.json")
        )
