# Swagger UI 静态资源（离线用）

`/docs` 页面从这里加载 JS/CSS，**不依赖外网 CDN**。
起因：FastAPI 默认的 `/docs` 从 `cdn.jsdelivr.net` 拉资源，国内网络常被重置，
浏览器拿不到 JS 就只剩白屏（接口本身正常）。

| 项 | 值 |
| --- | --- |
| 来源 | `swagger-ui-dist` 5.17.14（npm 包） |
| 许可证 | Apache-2.0（见同目录 `LICENSE` / `NOTICE`） |
| 引用位置 | `backend/app/openapi_docs.py` |

## 只保留必要的文件

- `swagger-ui.css`
- `swagger-ui-bundle.js`（核心 + APIs preset）
- `swagger-ui-standalone-preset.js`（顶栏 / Search / Servers）
- `favicon-32x32.png`
- `LICENSE`、`NOTICE`（许可证要求随发布附带）

其余文件（`*.map`、`swagger-ui.js`、`index.html` 等）都删掉了，节省 ~1.5MB。

## 升级方式

```bash
cd /tmp && rm -rf swagger-dl && mkdir swagger-dl && cd swagger-dl
curl -sSLO https://registry.npmmirror.com/swagger-ui-dist/-/swagger-ui-dist-<版本>.tgz -o dist.tgz
tar -xzf dist.tgz
cp package/{swagger-ui.css,swagger-ui-bundle.js,swagger-ui-standalone-preset.js,favicon-32x32.png,LICENSE,NOTICE} \
   <repo>/backend/app/static/swagger/
```

> 用 npmmirror 而不是 jsdelivr，原因同上：jsdelivr 在国内不可靠。
> 升级后刷新 `/docs` 验证页面正常、浏览器 Network 里三个资源都是 200。

## 校验（不打开浏览器也能测）

```bash
curl -s localhost:8000/docs | grep /static/swagger     # 应看到本地路径，不含 cdn.jsdelivr.net
curl -sI localhost:8000/static/swagger/swagger-ui-bundle.js | head -1   # 期望 200
```
