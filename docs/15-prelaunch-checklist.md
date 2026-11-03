# 15 上线前修改清单

> 用途：上线（或对外演示）前逐条过一遍。**A 类全绿才允许上线**，B 类可延后。
> A 类已经脚本化：`sudo ENV_FILE=/etc/planharness/planharness.env deploy/preflight.sh`
> 本地预演：`ENV_FILE=backend/.env deploy/preflight.sh --soft`

---

## 当前状态快照（2026-11-03）

| 项 | 现状 | 结论 |
| --- | --- | --- |
| 测试 | **58 passed**（真实 Postgres 集成用例已启用，不再 skip） | ✅ |
| `.env` 是否入 Git | 未跟踪（只跟踪 `.env.example`） | ✅ |
| `backend/.env` 的 `ENV` | 已改回 `dev`（本机 SQLite） | ✅ |
| `SECRET_KEY` | 本地两份 .env 均已随机化；**服务器待执行一条 `make-prod-env.sh`** | ⏳ 半步 |
| 数据库 | **本机已验证**：appuser + planharness 库 + 17 张表（13 业务 + 4 checkpoint）；服务器待跑 `create-db.sh` | ✅ 本机 |
| checkpointer 后端 | `auto` → 本机已实测 `PostgresSaver`，**跨进程 resume 成功** | ✅ |
| 备份 / 清理 timer | 脚本已就绪，**未部署** | ❌ 阻断 |
| `postgres` 超级用户收口 | 未做 | ❌ 阻断 |
| 端口暴露 | 未验证（本机无服务） | ❌ 待验证 |

---

### 本机 Postgres 实测（2026-11-03）

```
Postgres.app 16.15，角色 appuser / 库 planharness 已存在
→ 用 .env.prod 配置启动应用：建出 17 张表（13 业务 + 4 checkpoint）
→ 跨进程验证：
   进程1: status=pending_approval（挂起，进程退出）
   进程2: status=committed version=2 verified=True（全新进程恢复）
→ 库内结果：plan_versions=[(1,'superseded'), (2,'active')]，checkpoints=6 行
→ pytest 58 passed（Postgres 集成用例已启用）
```

这说明「重启后审批不丢」这条链路是真的能跑通的，而不只是设计。

---

## A. 阻断项

### A1 换掉 `SECRET_KEY`

**为什么**：它签 JWT。占位符 `CHANGE_ME_...` 在仓库里公开可查 → 任何人能伪造任意用户的 token，有效期 7 天。

**本地已完成**（`backend/.env` 与 `backend/.env.prod` 都已是 64 字符随机值）。
服务器上一条命令即可（会自动生成）：

```bash
sudo deploy/make-prod-env.sh --db-password '数据库密码'
```

**验证**：`preflight.sh` 里 `SECRET_KEY 已设置（>=32 字节）`。
**副作用**：换完旧 token 全失效，用户需重新登录（本地无所谓）。

---

### A2 生产环境变量

`/etc/planharness/planharness.env`，`640 root:planharness`：

```text
ENV=prod
DATABASE_URL=postgresql+psycopg://appuser:<密码>@127.0.0.1:5432/planharness
SECRET_KEY=<A1 生成的>
CORS_ORIGINS=https://www.agentworkout.online
DEEPSEEK_API_KEY=<生产专用 Key，或留空走模板兜底>
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TIMEOUT_S=30
CONTEXT_TOKEN_BUDGET=2000
CHECKPOINTER_BACKEND=auto
CHECKPOINTER_POOL_SIZE=5
LANGGRAPH_STRICT_MSGPACK=true
```

**注意**：密码含 `@ : / ? #` 必须 URL 编码。`ENV=prod` + `DATABASE_URL` 是 SQLite 会被 `preflight.sh` 判阻断。

---

### A3 建库建用户（不要用 postgres 跑应用）

**脚本已就绪，但还没在任何机器上执行过**（本机 5432 未启动）。

```bash
sudo deploy/create-db.sh --password "$(openssl rand -base64 24)" --print-env
psql "postgresql://appuser:密码@127.0.0.1:5432/planharness" -c 'select 1'
```

**验证**：`preflight.sh` 的 `DATABASE_URL 指向 Postgres`。

---

### A4 收口 `postgres` 超级用户

做法（不要删角色，删了没有救火入口）：

```conf
# pg_hba.conf
host    planharness   appuser   127.0.0.1/32   scram-sha-256
host    all           all       0.0.0.0/0      reject
local   all           postgres                peer     # 只能 docker exec 进去用
```

```bash
docker exec pg pg_ctl reload -D /var/lib/postgresql/data
```

**验证**：从另一台机器 `psql -h 服务器IP -U postgres` 应连不上。

---

### A5 端口只绑本机

```bash
docker inspect pg --format '{{json .HostConfig.PortBindings}}'   # 5432 应为 127.0.0.1
sudo ss -lntp | grep -E ':(80|443|9200|5432)\b'                   # 9200/5432 只见 127.0.0.1
```

---

### A6 环境文件权限

```bash
sudo chown root:planharness /etc/planharness/planharness.env
sudo chmod 640 /etc/planharness/planharness.env
```

**验证**：`preflight.sh` 的 `权限 640 root:planharness`。

---

### A7 备份 timer + 异地一份

```bash
sudo install -m 750 -o root -g root deploy/db-backup.sh /opt/planharness/deploy/db-backup.sh
sudo install -m 644 deploy/planharness-backup.service deploy/planharness-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now planharness-backup.timer
sudo /opt/planharness/deploy/db-backup.sh        # 立刻跑一次确认能出包
```

**异地**：`rsync` / `rclone` 到另一台机器或对象存储。**本机备份救不了"机器没了"**。

**恢复演练**：每季度一次，恢复到 `planharness_restore_test` 库，别覆盖生产。

---

### A8 checkpoint 清理 timer

```bash
sudo install -m 644 deploy/planharness-checkpoint-cleanup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now planharness-checkpoint-cleanup.timer
sudo -u planharness /opt/planharness/backend/.venv/bin/python -m app.agent.checkpoint_cleanup --days 30 --dry-run
```

---

### A9 DeepSeek Key 用独立的、限额的

开发用的 Key 不要带到生产。泄露的代价是账单。更稳的做法见 [08 AI Key 安全与网关](08-ai-gateway-security.md)。

**验证**：`journalctl -u planharness | grep -c 'sk-'` 应为 0。

---

### A10 打开 `LANGGRAPH_STRICT_MSGPACK`

```text
LANGGRAPH_STRICT_MSGPACK=true
```

checkpoint 走 msgpack 反序列化；数据库被攻破 + 无此开关 = 可能任意代码执行。

---

## B. 建议项（不阻断，但会被追问）

| # | 事项 | 对应技术债 | 成本 |
| --- | --- | --- | --- |
| B1 | 登录接口限流（防密码爆破） | #11 | 1 小时 |
| B2 | refresh token（access 30min + refresh 7d） | #10 | 2 小时 |
| B3 | 路由声明 `response_model`，`/docs` 才有响应结构 | #5 | 1 小时（牵前端） |
| B4 | `checkins` 挪进 `domain/checkins.py` | #4 | 15 分钟 |
| B5 | token 估算换成真实分词器或按 usage 校准 | #12 | 1 小时 |
| B6 | 用 evals 调截断阈值（现在 50/30 是拍的） | #13 | 半天 |
| B7 | `/docs` 是否对公网开放（演示需要就保留，否则 Nginx 加 basic auth） | — | 10 分钟 |
| B8 | `draft` 状态删掉或补草稿阶段（状态机与代码一致） | #6 | 5 分钟 |

> 完整技术债表在 [12 架构总结 §11](12-architecture.md)。

---

## C. 上线后 10 分钟验证

```bash
# 1) 服务活着
systemctl status planharness --no-pager
curl -sS http://127.0.0.1:9200/api/v1/health

# 2) 公网可达 + HTTPS
curl -sS https://www.agentworkout.online/api/v1/health

# 3) 文档页不是白屏
curl -sS http://127.0.0.1:9200/docs | grep -c static/swagger   # 应 > 0

# 4) 建表齐全（13 张业务表 + 4 张 checkpoint 表）
psql "postgresql://appuser:密码@127.0.0.1:5432/planharness" -c '\dt'

# 5) 真实登录 + 读计划（端到端）
TOKEN=$(curl -s -X POST https://www.agentworkout.online/api/v1/auth/demo \
  -H 'Content-Type: application/json' -d '{}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["access_token"])')
curl -sS https://www.agentworkout.online/api/v1/plans/current -H "Authorization: Bearer $TOKEN" | head -c 200

# 6) 备份产出了
sudo ls -lh /var/backups/planharness/ | tail -3

# 7) 没有密钥泄漏进日志
sudo journalctl -u planharness --since '10 min ago' | grep -c 'sk-'    # 应为 0
```

---

## D. 回滚预案

```bash
# 代码回滚
cd /opt/planharness && git log --oneline -5
git checkout <上一个稳定提交> && systemctl restart planharness

# 数据库回滚（只在数据被写坏时）
gunzip -c /var/backups/planharness/planharness-<时间>.sql.gz \
  | docker exec -i pg psql -U appuser -d planharness
```

**顺序**：先回滚服务（分钟级）→ 再评估是否需要回滚数据（小时级，且有丢失窗口）。

---

## E. 建议的一次性处理顺序（约 90 分钟）

```
1. A1 生成 SECRET_KEY                      2 分钟
2. A3 建库建用户                            10 分钟
3. A2 写生产 env 文件                       5 分钟
4. A4 收口 postgres + reload                10 分钟
5. 部署代码 + venv + systemd 首次启动        20 分钟
6. 首次启动自动建表 → 用 C 的 4) 核对        5 分钟
7. A7 备份 timer + 手动跑一次                10 分钟
8. A8 清理 timer                            5 分钟
9. A5/A6 权限与端口核对                      5 分钟
10. 跑 preflight.sh，全绿                   2 分钟
11. C 的 7 条验证                           10 分钟
```

如果时间被压缩，**不能跳过的只有**：A1（密钥）、A7（备份）、A5（端口）。其余可以第二天补。
