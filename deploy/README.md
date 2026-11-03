# PlanHarness 生产部署：systemd + Nginx + 现有 Postgres 容器

> 适用当前服务器：Ubuntu 24.04，2C2G，已有 Nginx，已有 `pg` 容器，API 不跑容器。  
> **不要运行 `docker-compose.example.yml`**：它会再起 Postgres/Caddy，和现有 Nginx/端口冲突。

## 1. 结论

你的架构是合理的：

```text
浏览器 → Nginx :80/:443 → uvicorn 127.0.0.1:9200 → Postgres 127.0.0.1:5432
```

- 只暴露 80/443，正确。
- API 绑 127.0.0.1，正确。
- Postgres 只映射 127.0.0.1，正确。
- 2C2G 不适合再跑 Caddy + API 容器，复用宿主 systemd 更稳。

需要修正的重点：

1. **API 不要用 root 跑**；给它专用用户 `planharness`。
2. `.env` 放到 `/etc/planharness/planharness.env`，`640 root:planharness`。
3. `DATABASE_URL` 用现有 `appuser`，不是 compose 里的 `planharness` 用户。
4. systemd 加 `Restart=always`、`NoNewPrivileges`、`ProtectSystem`、`ProtectHome`。
5. Nginx 加 `X-Forwarded-*`、120s 读超时、限流。
6. uvicorn 只开 **1 worker**，2C2G 不要开多 worker。

## 2. 先别动：过 22:00 Gate

今晚先保证现有服务能跑：

```bash
systemctl status <当前服务名> --no-pager
journalctl -u <当前服务名> -n 100 --no-pager
curl -sS https://www.agentworkout.online/api/v1/health
```

Gate 过了，明天上午再做非 root 迁移，避免今晚把可用服务弄挂。

## 3. 迁移步骤

### 3.1 建用户和目录

```bash
sudo useradd --system --home /opt/planharness --shell /usr/sbin/nologin planharness || true
sudo mkdir -p /opt/planharness /etc/planharness
sudo chown -R planharness:planharness /opt/planharness
```

### 3.2 停旧服务并复制代码

```bash
sudo systemctl stop <当前服务名>
sudo rsync -a --exclude '.venv' --exclude '.env' --exclude '__pycache__' \
  /root/planharness/ /opt/planharness/
sudo chown -R planharness:planharness /opt/planharness
```

### 3.3 重建 venv（用 planharness 用户）

```bash
sudo -u planharness python3.12 -m venv /opt/planharness/backend/.venv
sudo -u planharness /opt/planharness/backend/.venv/bin/pip install --upgrade pip
sudo -u planharness /opt/planharness/backend/.venv/bin/pip install /opt/planharness/backend
```

> 如果你用 uv：`sudo -u planharness uv sync`，确保 uv 对 planharness 用户可用。

### 3.4 环境文件

生产配置**只存在于服务器**，仓库里不放：

```bash
sudo deploy/make-prod-env.sh --db-password '刚建的数据库密码'
```

它会自动：生成 48 字节随机 `SECRET_KEY`、把密码 URL 编码后拼进 `DATABASE_URL`、
写入 `/etc/planharness/planharness.env`、权限设成 `640 root:planharness`，
最后打印一份打码摘要供核对。

需要改域名/Key 时：

```bash
sudo deploy/make-prod-env.sh --db-password '密码' --domain www.agentworkout.online \
    --deepseek-key sk-xxxx --force
```

字段清单见 [`deploy/planharness.env.example`](planharness.env.example)（不含任何真实值）。

> **为什么不用仓库里的 `.env`？**
> 配置的加载顺序是「环境变量 > `.env`（按启动目录找）> 代码默认值」。
> 仓库里放 `.env` 会因为"从哪个目录启动"而读到不同内容，而且很容易被误提交。
> 服务器上让 systemd 用 `EnvironmentFile=` 注入，仓库目录里**一个 `.env` 都不要有**。

注意：

- 密码里如果有 `@ : /`，先 URL 编码。
- `chmod 640`，owner `root:planharness`；systemd 以 root 读取 EnvironmentFile，再降权给 planharness。
- 不要把 `.env` 放回代码目录，也不要提交 Git。

### 3.4.1 建库与建用户

先有一个专用角色和库（**不要用 postgres 超级用户跑应用**）：

```bash
sudo deploy/create-db.sh --password "$(openssl rand -base64 24)" --print-env
```

它会调 `deploy/init-db.sql`：建 `appuser`（只登录、不超级）、建库并归它所有、收紧 `public` schema。
`--print-env` 会直接打印可粘贴的 `DATABASE_URL`（密码已做 URL 编码）。

想自己跑 SQL 也行：

```bash
psql -U postgres -v app_password='你的密码' -f deploy/init-db.sql
```

脚本做三件事：建 `appuser`（只登录、不超级）、建 `planharness` 库归它所有、收紧 `public` schema 权限。

**密码要不要设？** 要。原因：

- 生产环境 `pg_hba.conf` 对 TCP 连接（`127.0.0.1`）默认要求 `scram-sha-256`，没密码连不上；
- 本地 Homebrew / Postgres.app 默认 `trust` 或 `peer`，能免密连，但**本地免密、线上要密码**会让"本机跑得好好的"变成上线第一坑；
- 最小权限：`appuser` 只拥有 `planharness` 库，误操作炸不到别的库。

> 密码里如果含 `@ : / ? #` 等字符，写进 `DATABASE_URL` 前必须 URL 编码，
> 否则会被当成 URL 分隔符解析（常见症状：连接串看起来对，但认证失败或连到错的库）。

### 3.4.2 业务表和 checkpoint 表从哪来

**不需要手写建表语句**，两个来源都在应用启动时自动执行：

| 表 | 由谁创建 | 时机 |
| --- | --- | --- |
| `users` / `plans` / `plan_patches` …… | `Base.metadata.create_all()` | 应用启动时（`app/main.py`） |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` | `saver.setup()` | 应用启动时，且 checkpointer 解析为 postgres |

`setup()` 执行的是 `langgraph-checkpoint-postgres` 包里自带的 10 条迁移（4 张表 + 索引 + ALTER），
带 `IF NOT EXISTS` 和版本表 `checkpoint_migrations`，所以**重复执行是安全的**。

不想启动整个应用、只想先建表也可以：

```bash
cd /opt/planharness/backend
.venv/bin/python -c "
from langgraph.checkpoint.postgres import PostgresSaver
DSN = 'postgresql://appuser:密码@127.0.0.1:5432/planharness'
with PostgresSaver.from_conn_string(DSN) as saver:
    saver.setup()
    print('checkpoint 表已就绪')
"
```

建完确认：

```bash
psql "postgresql://appuser:密码@127.0.0.1:5432/planharness" -c '\dt'
# 应看到 checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations
# 以及 users / plans / plan_versions / ...
```

### 3.5 systemd

```bash
sudo cp deploy/planharness.service /etc/systemd/system/planharness.service
sudo systemctl daemon-reload
sudo systemctl enable --now planharness
sudo systemctl status planharness --no-pager
```

如果 `ProtectSystem=strict` 导致应用无法写缓存/临时文件，先看日志：

```bash
journalctl -u planharness -n 100 --no-pager
```

需要写路径时再加：

```ini
ReadWritePaths=/opt/planharness/backend
```

### 3.5.1 checkpoint 清理定时任务

LangGraph 的 `checkpoints` / `checkpoint_writes` / `checkpoint_blobs` 三张表只增不减，
需要定期清理「N 天没有活动」的 thread（详见 `backend/app/agent/checkpoint_cleanup.py`）。

先手动试跑：

```bash
sudo -u planharness /opt/planharness/backend/.venv/bin/python \
  -m app.agent.checkpoint_cleanup --days 30 --dry-run
# 输出示例：threads=42 pruned=0 dry_run=True
```

确认命中数量合理后再真正删除（去掉 `--dry-run`）。挂成每周一次的 systemd timer：

```ini
# /etc/systemd/system/planharness-checkpoint-cleanup.service
[Unit]
Description=PlanHarness checkpoint cleanup
After=network.target

[Service]
Type=oneshot
User=planharness
WorkingDirectory=/opt/planharness/backend
EnvironmentFile=/etc/planharness/planharness.env
ExecStart=/opt/planharness/backend/.venv/bin/python -m app.agent.checkpoint_cleanup --days 30
```

```ini
# /etc/systemd/system/planharness-checkpoint-cleanup.timer
[Unit]
Description=Weekly checkpoint cleanup

[Timer]
OnCalendar=Sun 04:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now planharness-checkpoint-cleanup.timer
systemctl list-timers --no-pager | grep checkpoint
```

> 注意：`--days` 不要小于业务上「审批最长挂多久」。审批规则是人工处理，
> 保留 30 天足够；如果挂起中的 thread 被清掉，用户点「通过」时会找不到现场。

### 3.6 Nginx

```bash
sudo cp deploy/nginx-planharness.conf /etc/nginx/conf.d/planharness.conf
sudo nginx -t
sudo systemctl reload nginx
```

如果已有 `server_name www.agentworkout.online` 的配置，先合并/删除旧的，避免重复 server 块。

证书路径默认：

```text
/etc/letsencrypt/live/www.agentworkout.online/fullchain.pem
/etc/letsencrypt/live/www.agentworkout.online/privkey.pem
```

没有证书：

```bash
sudo certbot --nginx -d www.agentworkout.online
```

### 3.7 静态 H5 权限

```bash
sudo chmod -R a+rX /opt/planharness/frontend/dist
```

Nginx 以 `www-data` 读取；目录需要 `x`，文件需要 `r`。

## 3.8 上线前自检

```bash
sudo ENV_FILE=/etc/planharness/planharness.env /opt/planharness/deploy/preflight.sh
```

阻断项（密钥占位符 / 非 Postgres / 端口暴露 / env 权限 / 定时任务）任一失败都会返回非 0。
完整清单见 [docs/15 上线前修改清单](../docs/15-prelaunch-checklist.md)。

## 4. 验证

```bash
# 只应看到 80/443 对公网，9200/5432 只在 127.0.0.1
sudo ss -lntp | grep -E ':(80|443|9200|5432)\b'

# API 服务
systemctl status planharness --no-pager
journalctl -u planharness -n 50 --no-pager

# 本机健康检查
curl -sS http://127.0.0.1:9200/api/v1/health

# 文档页：应输出 /static/swagger/... 而不是 cdn.jsdelivr.net
curl -sS http://127.0.0.1:9200/docs | grep static/swagger

# 公网健康检查
curl -sS https://www.agentworkout.online/api/v1/health

# Postgres 容器端口绑定
docker inspect pg --format '{{json .HostConfig.PortBindings}}'
```

期望：

- `80/443` 绑定 `0.0.0.0`
- `9200/5432` 绑定 `127.0.0.1`
- `/api/v1/health` 返回 `{"code":0,...}`

## 5. 回滚

```bash
sudo systemctl stop planharness
sudo systemctl start <旧服务名>
sudo nginx -t && sudo systemctl reload nginx
```

不要删除旧目录，直到新服务验证通过。

## 6. 2C2G 运行建议

- uvicorn `--workers 1`，不要 2+。
- `--limit-concurrency 20`。
- Postgres `shared_buffers=128MB`、`max_connections=20`。
- 不在服务器上 `npm build`，本地 build 后传 `dist`。
- 监控：

```bash
free -h
docker stats --no-stream
systemctl status planharness
```

- 如果 API 常驻内存超过 500MB，先查 LangGraph/依赖泄漏，不要盲目加 worker。

## 6.1 备份与恢复（被攻破时的唯一底牌）

收权限只能降低「被攻破的概率」，**能不能恢复**才决定「被攻破会不会报废」。

```bash
# 1) 部署脚本
sudo install -m 750 -o root -g root deploy/db-backup.sh /opt/planharness/deploy/db-backup.sh
sudo install -m 644 deploy/planharness-backup.service /etc/systemd/system/
sudo install -m 644 deploy/planharness-backup.timer   /etc/systemd/system/

# 2) 先手动跑一次，确认能出包
sudo /opt/planharness/deploy/db-backup.sh
sudo ls -lh /var/backups/planharness/

# 3) 开启每日 03:30 自动备份
sudo systemctl daemon-reload
sudo systemctl enable --now planharness-backup.timer
systemctl list-timers --no-pager | grep planharness
```

默认保留 14 天（`KEEP_DAYS` 可覆盖），备份目录 `/var/backups/planharness`。

**异地一份**：本机备份救不了「整台机器没了 / 磁盘被加密」。最低成本做法：

```bash
# 每天把最新备份同步到另一台机器或对象存储
rsync -avz /var/backups/planharness/ backup@another-host:/backups/planharness/
# 或：rclone copy /var/backups/planharness remote:planharness-backup
```

**恢复演练**（每季度做一次，否则你不知道备份能不能用）：

```bash
# 在测试库上恢复，不要直接覆盖生产
gunzip -c /var/backups/planharness/planharness-YYYYmmdd-HHMMSS.sql.gz \
  | docker exec -i pg psql -U appuser -d planharness_restore_test
```

## 7. 安全清单

- [ ] API 以 `planharness` 用户运行，不是 root
- [ ] `.env` 640 root:planharness，不在 Git
- [ ] 9200/5432 仅 127.0.0.1
- [ ] Nginx 只开 80/443
- [ ] `NoNewPrivileges` / `ProtectSystem` 已开
- [ ] DeepSeek Key 低余额、专用
- [ ] Nginx 限流已开
- [ ] 日志不打印 Authorization/Key
- [ ] `grep -R "sk-" /opt/planharness /etc/planharness` 无结果
- [ ] 应用连库用 `appuser`，**不用 `postgres` 超级用户**
- [ ] `postgres` 角色只能本地 socket 登录（`pg_hba.conf`: `local all postgres peer`），禁止 TCP
- [ ] 5432 不对公网暴露（`docker inspect pg` 确认只绑 127.0.0.1）
- [ ] 每日备份已开，且**异地有一份**；最近一次恢复演练在 3 个月内
- [ ] `/proc` 加固已开（`ProtectProc=invisible`），减少环境变量被同机进程读取
- [ ] 服务器 SSH 只允许密钥登录，禁用 root 密码登录
- [ ] `.env` 里没有把 DeepSeek Key 打进日志（检查 `journalctl -u planharness | grep -c sk-`）
