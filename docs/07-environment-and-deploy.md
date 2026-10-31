# 07 环境与部署

> 记录时间：2026-10-31 14:50（Asia/Shanghai）

## 1. 当前环境

| 项 | 状态 | 结论 |
| --- | --- | --- |
| 域名 | `www.agentworkout.online`，备案完成 | 可用于 H5 / 小程序 request 域名 |
| LLM | DeepSeek | `base_url=https://api.deepseek.com/v1`，`model=deepseek-chat` |
| 服务器 | 无 Docker，可安装 | 优先装 Docker + Compose；失败则 systemd 原生部署 |
| 小程序 AppID | 没有 | 真实微信登录/真机预览本周不可用；H5 为主，代码路径保留 |
| HTTPS | 未配置 | Caddy 自动签发 Let's Encrypt |
| 数据库 | 未定 | Postgres（Docker）；本地可用 SQLite 过渡 |

## 2. 域名与 HTTPS

### DNS

在域名服务商添加：

```text
类型  A
主机  www
值    <服务器公网 IPv4>
TTL   600
```

可选：apex `@` 也指向同一 IP，Caddy 做 301 到 `https://www.agentworkout.online`。

### 端口

- 安全组/防火墙放行 `80`、`443`。
- 应用端口（如 8000）不要对公网开放，只给 Caddy 反代。

### Caddy

Caddy 自动申请证书，配置目标：

```text
www.agentworkout.online {
    encode gzip
    reverse_proxy api:8000
}
```

H5 静态文件可由 Caddy `file_server` 或 Nginx 提供；MVP 建议 Caddy 同时服务 H5 和 API：

```text
www.agentworkout.online {
    handle /api/* {
        reverse_proxy api:8000
    }
    handle {
        root * /srv/web
        try_files {path} /index.html
        file_server
    }
}
```

## 3. DeepSeek

### 环境变量

写入服务器 `.env`，不要提交 Git，也不要发在聊天里：

```text
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_TIMEOUT_S=30
```

### 调用约定

- 计划生成：一次结构化 JSON 输出；失败自动模板兜底。
- 补丁生成：一次 JSON 输出；失败模板补丁。
- 周复盘解释：一次短输出；规则由代码算。
- `temperature=0`，保证演示可复现。
- 不在 SDK 里做隐式重试；重试由图的节点控制，最多 2 次。
- 记录 `tokens_in/out`、`latency_ms`、`model` 到 `agent_runs`。

### 安全

- Key 只放服务端 `.env` / 密钥管理；前端永远不接触。
- 日志不打印 Key、Authorization header。
- 演示数据使用合成数据；不把真实用户健康数据发给第三方模型。

## 4. 服务器与 Docker

### 建议

Ubuntu 22.04/24.04，2C4G 起步；安装 Docker Engine + Compose Plugin。

```bash
# 官方仓库方式（示例）
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

验证：

```bash
docker --version
docker compose version
```

### Compose 服务

- `api`：FastAPI + Uvicorn
- `db`：Postgres 16 + volume
- `caddy`：80/443 + H5 静态文件
- 可选 `redis`：限流/缓存，MVP 不需要

### Docker 装不上时

原生部署：

- Postgres：`apt install postgresql`
- API：`systemd` + `uvicorn`
- Caddy：`apt install caddy`
- 仍然用 Caddy 做 HTTPS 和反代

## 5. 小程序：没有 AppID 的处理

### 现实

没有 AppID，本周无法完成：

- 真实微信登录（`wx.login` → `code2session`）
- 真机预览/体验版
- 微信审核/发布

这不是代码问题，是账号/资质问题。

### 本周策略

1. **H5 是主演示**：`https://www.agentworkout.online`，手机浏览器可用。
2. 小程序代码照写，用微信开发者工具的测试号/游客模式跑本地模拟器。
3. 页面/交互截图或录屏，作为“多端代码已就绪”的证据。
4. 今天就注册个人小程序（如果还没注册），AppID 下来后：
   - 填 AppID/AppSecret；
   - 配置 request 合法域名 `https://www.agentworkout.online`；
   - 接 `wx.login`；
   - 真机预览 0.5 天可完成。

### 注册路径

微信公众平台 → 立即注册 → 小程序 → 个人主体 → 邮箱激活 → 身份验证 → 填写名称/服务类目（运动健身/工具）→ 获取 AppID。

> 审核时间不受你控制，所以**不要把小程序放在本周关键路径上**。H5 先跑通，小程序代码并行。

## 6. 部署 Check List

- [ ] DNS `www` 指向服务器 IP
- [ ] 80/443 放行
- [ ] Docker + Compose 安装成功
- [ ] `.env` 写入 DeepSeek Key，权限 600
- [ ] Postgres volume 持久化
- [ ] Caddy 证书签发成功
- [ ] `https://www.agentworkout.online/api/v1/health` 返回 ok
- [ ] H5 首页可打开
- [ ] demo 数据重置可用
- [ ] 日志不包含 Key/健康隐私
- [ ] 小程序 request 域名已配置（有 AppID 后）

## 7. 对一周排期的影响

| 项 | 影响 | 处理 |
| --- | --- | --- |
| 无小程序 AppID | 真实登录/真机不可用 | H5 主演示；小程序代码/模拟器并行；AppID 下来后接 |
| 无 Docker | 部署多 30～60 分钟 | 今天装；失败走原生 |
| DeepSeek Key | 真实模型依赖 Key | Key 写服务器 `.env`；本地可用 FakeLLM |
| 域名已备案 | 省 1～3 天 | Caddy 直接签证书 |

**结论：本周关键路径改为 H5 + API + Harness；小程序是并行证据，不阻塞演示。**
