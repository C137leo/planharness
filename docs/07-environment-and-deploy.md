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

**不要把 Key 发在聊天里，也不要提交 Git。** 生产推荐把 Key 放在单独文件里，
只把文件路径写进环境变量：

```bash
# 文件权限：root 可写，服务用户只读
install -m 640 -o root -g planharness /dev/null /etc/planharness/deepseek.key
# 用编辑器写入 Key（不要用会进 shell history 的 echo 命令）
nano /etc/planharness/deepseek.key
```

```text
# /etc/planharness/planharness.env
DEEPSEEK_API_KEY_FILE=/etc/planharness/deepseek.key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_TIMEOUT_S=30
```

应用启动时把文件内容读入内存，不再把 Key 放进进程环境；skill 子进程也只拿到
白名单环境（PATH/HOME/locale/SKILL_*），不会继承 DeepSeek Key。

开发/临时仍兼容直接写 `DEEPSEEK_API_KEY=sk-...`；两者同时存在时直接写的优先。

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

### 生产日志与排障

应用日志走 systemd journal，不另写日志文件；Nginx 请求日志在 `/var/log/nginx/`。

```bash
# 最近 200 行 / 实时跟随
journalctl -u planharness-api -n 200 --no-pager
journalctl -u planharness-api -f

# 只看报错和堆栈
journalctl -u planharness-api --since '30 min ago' | grep -Ei 'error|traceback|exception|failed'

# Nginx
tail -n 100 /var/log/nginx/error.log
tail -f /var/log/nginx/access.log
```

现在每个 `/api/` 请求都会在 journal 里留一条：

```text
INFO: trace_id=261537... GET /api/v1/agent/today-plan -> 200 23ms
```

响应信封和 `X-Trace-Id` 响应头也带同一个 `trace_id`；5xx 时前端错误文案会附上它。
拿 `trace_id` 直接搜：

```bash
journalctl -u planharness-api --since '30 min ago' | grep 261537
```

Console 的 run 详情还能看 `trace`、`error_code` 和成本。若 `output.fallback=true`：
这次没有调用模型，走的是确定性兜底，所以 `tokens_in/out=0`、成本 `0` 是正确的，
不是计费丢失。检查模型 Key 是否已配置：

```bash
# 推荐方式：文件是否非空（不会打印 Key 本身）
sudo test -s /etc/planharness/deepseek.key && echo "key file: set" || echo "key file: empty"
# 旧方式：环境变量是否非空
sudo grep '^DEEPSEEK_API_KEY=' /etc/planharness/planharness.env | awk -F= '{print length($2)}'

# 改完 Key 或 env 后
sudo systemctl restart planharness-api
```

### 大模型总闸（运营开关）

Console 顶部有一个只对 `LLM_ADMIN_PHONE`（默认 `13772692821`）显示的开关：

- 打开：写入 `runtime_flags.llm_paused=true`，所有 `/api/` 请求里的大模型调用在
  `ToolCallingClient` / `DeepSeekExplainer` 这一层被拦住，不请求 DeepSeek；
  所有用户会看到「该项目正在修改中，暂时无法调用大模型，请等待通知后再使用~」，
  今日规划/计划草案/改计划等入口直接返回 503。
- 关闭：恢复模型调用，token 与费用重新开始累计。

开关不需要重启；状态每 30 秒由前端轮询一次。审计链 `system:llm` 会记录谁在什么时候
暂停/恢复。

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
