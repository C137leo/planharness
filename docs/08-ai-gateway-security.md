# 08 AI Key 安全与网关

> 目标：**应用服务器不持有 DeepSeek 长期 Key**；即使服务器被入侵，攻击者也拿不到 Key，只能拿到可撤销、可限额的网关 token。

## 1. 先给结论

不要把 DeepSeek Key 写进应用服务器的 `.env`。

推荐：

```text
浏览器/小程序
      ↓
应用服务器（只持有 GATEWAY_TOKEN）
      ↓
AI Gateway（Cloudflare Worker / 腾讯云 SCF，持有 DEEPSEEK_API_KEY）
      ↓
DeepSeek API
```

- DeepSeek Key 只存在网关的 Secret 里。
- 应用服务器只拿到一个 **低权限、可撤销、有限额** 的 `GATEWAY_TOKEN`。
- 网关做 IP 白名单、限流、模型白名单、max_tokens、预算和审计。

## 2. 威胁模型：能防什么，不能防什么

| 威胁 | 直连 + `.env` | AI Gateway |
| --- | --- | --- |
| Key 被提交到 Git/前端 | 高风险 | Key 不在仓库/前端 |
| 服务器磁盘/快照泄露 | Key 可读 | Key 不在应用服务器 |
| 应用服务器被入侵 | Key 被偷，可长期盗刷 | 只能拿到 gateway token，可撤销/限额 |
| 日志误打印 | Key 可能泄露 | Key 不在应用进程内 |
| 内部人员 | 可读 Key | 只能看到网关 token |
| 服务器 root 被完全控制 | 无法防 | 仍可在限额内盗刷，但拿不到 DeepSeek Key |

**结论：网关能防止 Key 被“偷走”，不能防止“被滥用”。**  
所以还要限额、告警、轮换。

## 3. 推荐方案：Cloudflare Worker AI Gateway

优点：

- 不需要把域名 DNS 迁到 Cloudflare，直接用 `*.workers.dev` 地址。
- Key 存在 Worker Secret，不落应用服务器。
- 免费额度足够 demo。
- 改一行 base_url 就能接。

缺点：

- 国内服务器访问 `workers.dev` 可能慢/不稳；先测。
- 需要 Cloudflare 账号。

### 3.1 Worker 职责

只暴露一个接口：

```text
POST /v1/chat/completions
```

处理逻辑：

1. 校验 `Authorization: Bearer <GATEWAY_TOKEN>`，常量时间比较。
2. 校验来源 IP 是否在允许列表（应用服务器 `120.53.242.254`）。
3. 只允许 `model=deepseek-chat`；`max_tokens <= 2000`。
4. 限流：例如 60 次/分钟、2000 次/天。
5. 转发到 `https://api.deepseek.com/v1/chat/completions`，带上 Worker Secret 里的 DeepSeek Key。
6. 30 秒超时；只记录 request_id、状态码、tokens，不记录 prompt/completion。
7. 返回 DeepSeek 的响应；不返回任何 Key。

### 3.2 应用服务器配置

```text
DEEPSEEK_BASE_URL=https://<worker>.workers.dev/v1
DEEPSEEK_API_KEY=<GATEWAY_TOKEN>
DEEPSEEK_MODEL=deepseek-chat
```

注意：这里的 `DEEPSEEK_API_KEY` 是网关 token，不是 DeepSeek Key。  
即使泄露，攻击者也只能在限额内调用，且可以立即轮换。

### 3.3 部署步骤

1. Cloudflare 创建 Worker。
2. 设置 Secret：`DEEPSEEK_API_KEY`。
3. 设置 Secret：`GATEWAY_TOKEN`（随机 32 字节以上）。
4. 绑定限流/KV（可选；MVP 可用 Worker 内存计数 + Cloudflare Rate Limiting）。
5. 部署，拿到 `https://xxx.workers.dev`。
6. 在服务器上 `curl` 一次，确认返回 200、401（错 token）、429（超限）。
7. 应用只配置 Worker URL + gateway token。

### 3.4 轮换

- DeepSeek Key：只在网关后台换；应用无感。
- Gateway Token：怀疑泄露时立即换；应用重启加载新值。
- 演示结束：删除/降额 DeepSeek Key，清空网关预算。

## 4. 备选方案：腾讯云 SCF / SSM

如果 `workers.dev` 在国内服务器访问不稳定，用腾讯云：

### 方案 B1：SCF 函数做网关

- SCF 环境变量存 DeepSeek Key。
- 函数 URL 只接受带 `GATEWAY_TOKEN` 的请求。
- 应用服务器调用 SCF 函数 URL。
- 优点：同区域低延迟；Key 不在 CVM。
- 缺点：函数 URL 鉴权、限流要自己写；冷启动。

### 方案 B2：SSM 凭据管理 + CAM 角色

- Key 存腾讯云 SSM；CVM 绑定 CAM 角色，应用启动时拉取。
- Key 不落磁盘，但会进进程内存。
- 服务器被完全控制时，攻击者可用实例角色拉取。
- 适合“防磁盘/仓库泄露”，不适合“防服务器入侵”。

## 5. 如果坚持直连：最低限度保护

不推荐，但至少做到：

1. 专用 Linux 用户运行应用，禁止 root 运行。
2. `.env` 放 `/etc/planharness/planharness.env`，`chmod 600`，owner 为应用用户；不要放 Web 根目录。
3. systemd `LoadCredential=` 或 Docker Secret；不要写进镜像、`docker-compose.yml` 明文、构建参数。
4. 日志脱敏：不打印 `Authorization`、请求体、响应体；只打印 request_id。
5. 出站防火墙只允许 `api.deepseek.com:443`。
6. 独立 Key + 低余额 + 用量告警；演示后轮换。
7. `grep -R "sk-"` 检查仓库和部署目录；`.gitignore` 包含 `.env`。

**但这仍然防不了 root 被控制。** 所以首选网关。

## 6. 服务器加固（2C2G / Ubuntu 24.04）

截图信息：2 核 / 2GB / 50GB，已用 24.8GB；流量 300GB。够 demo，但要注意内存。

- 加 2GB swap。
- Docker 限制内存：Postgres 512MB、API 512MB、Caddy 128MB。
- Postgres `shared_buffers=128MB`、`max_connections=20`。
- 前端在本地 build，服务器只放 `dist`，不要在 2GB 机器上跑 npm build。
- 只开 22/80/443；不要暴露 5432/8000。
- SSH 只允许密钥登录，禁用密码/root 登录。
- 安装 fail2ban；定期 `apt upgrade`。
- 检查预装的 OpenClaw 是否在监听端口；不用就停掉，减少攻击面。
- Docker 日志限制：`max-size=10m`、`max-file=3`。

## 7. 今天的操作顺序

1. 在 DeepSeek 后台新建一把**专用 Key**，余额调低。
2. 部署 Cloudflare Worker；设置 `DEEPSEEK_API_KEY` + `GATEWAY_TOKEN`。
3. 服务器只配置 Worker URL + `GATEWAY_TOKEN`。
4. 用错 token、错 IP、超限各测一次，确认网关能拦。
5. 服务器加固：UFW、SSH、swap、Docker 内存限制。
6. 检查仓库/日志中没有 `sk-` 开头的 DeepSeek Key。

## 8. 对项目的配置影响

- `DEEPSEEK_BASE_URL` 从 `https://api.deepseek.com/v1` 改成网关地址。
- 代码不用改：仍然是 OpenAI 兼容 `/chat/completions`。
- `agent_runs` 记录 gateway request_id，便于对账。
- 评测/本地开发可用 FakeLLM；没有网关也能跑。
