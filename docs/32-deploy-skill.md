# 线上部署：skill（healthy-fitness-coach）怎么跟着走

## 1. 先看它是什么

- **纯 Node、无 npm 依赖**：只 `require('node:fs')` / `path` / `crypto` + 自己的相对路径文件；
- **体积**：仓库 6.3MB（核心 `healthy-fitness-coach/` 1.3MB，`agent-integration/` 44KB，`web/lib` 24KB）；
- **协议**：MIT（`LICENSE`，可直接随镜像分发，保留版权声明即可）；
- **调用方式**：`SkillBridge` 启动 `node runner.js <command>`，stdin 传 JSON、stdout 收 JSON，**每次调用约 60ms**，纯函数结果在进程内缓存。

所以它不像"要装依赖的 npm 服务"，更像"一个可以被复制的本地计算引擎"。

## 2. 三种部署方式

| 方式 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. Vendor 进镜像（推荐）** | `deploy/vendor-skill.sh` 把 skill 复制到 `backend/vendor/`，镜像里 `COPY vendor` | 单一制品、版本固定、无外网依赖 | 仓库/镜像多几 MB，skill 升级要重新构建 |
| B. 挂载卷 | 把 skill 放在宿主机，`-v /srv/skill:/app/skill`，配 `SKILL_DIR` | 不用重建镜像 | 依赖宿主机状态，容易"能跑但版本不对" |
| C. 独立 Node 服务 | 把 runner 包成 HTTP 服务，bridge 改成 HTTP 调用 | 无进程启动开销、可独立扩容 | 多一个服务要运维，当前 QPS 完全用不上 |

当前代码默认支持 A/B：只认环境变量 `SKILL_DIR`，找不到就降级（见第 4 节）。

## 3. 上线步骤（方式 A）

```bash
# 1) 把 skill 收进仓库（一次性/每次升级时）
./deploy/vendor-skill.sh ~/PycharmProjects/healthy-fitness-coach-skill
#   → backend/vendor/healthy-fitness-coach-skill/
#   → 记录 SKILL_COMMIT
#   → 自动重跑 scripts/sync_exercise_catalog.py（302 个动作的静态快照）

# 2) 构建镜像（Dockerfile 已装 node、COPY vendor、设 SKILL_DIR）
cd backend && docker build -t planharness-api .

# 3) 服务器环境变量（/etc/planharness/planharness.env）
SKILL_DIR=/app/vendor/healthy-fitness-coach-skill
SKILL_NODE=            # 留空 = PATH 里的 node
SKILL_TIMEOUT=20

# 4) 起服务并自检
curl -s https://<host>/api/v1/health | jq .data.skill
# 期望：{"available": true, "version": "...", "dir": "...", "node": "/usr/bin/node"}
```

`backend/Dockerfile` 里新增的关键几行：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
COPY vendor ./vendor
ENV SKILL_DIR=/app/vendor/healthy-fitness-coach-skill
```

`vendor/` 里放了一个 `.gitkeep`，**没 vendor 也能构建**（只是运行时会降级），不会因为缺目录把 CI 卡死。

## 4. 找不到 skill 会怎样（降级路径）

`SkillBridge.available` 为 False 时，`_run()` 返回：

```json
{"status": "unavailable", "error": "skill_not_available"}
```

上层行为：

- 规划接口**不 500**，改走本地确定性编排（`_deterministic_flow`），并在草案 `meta` 里标
  `skill_available: false`、`model_fallback: true`；
- 今日规划/补丁照常工作（它们不依赖 skill）；
- 健康检查里 `skill.available=false` + `dir`，运维一眼能看出是"没挂目录/没装 node"。

**这是有意的**：skill 挂了应该"能力降级"，而不是整站 502。

## 5. 版本与一致性

- `deploy/vendor-skill.sh` 会把 skill 的 git commit 写进 `vendor/.../SKILL_COMMIT`；
  线上出问题先看这个文件，能对上"当时用的是哪版"；
- 动作目录（`app/domain/exercise_catalog.py`）是**生成后提交**的静态快照，
  vendor 脚本会自动重跑 `sync_exercise_catalog.py`，避免"skill 加了动作、Python 侧还找不到"；
- skill 更新流程：`git pull` skill → `vendor-skill.sh` → 跑测试（`test_coach.py`、evals E09/E10）→ 构建镜像。

## 6. CI 里建议加的三步

```bash
./deploy/vendor-skill.sh "$SKILL_CHECKOUT"
cd backend && .venv/bin/python -m pytest tests/test_coach.py tests/test_evals.py -q
docker build -t planharness-api:ci .
curl -sf localhost:8000/api/v1/health | grep -q '"available": true'
```

第三条（健康检查断言 skill 可用）能挡住"镜像里忘了装 node"这类事故。

## 7. 什么时候该换成方式 C

- 单实例 QPS 高（每次规划要调 3–6 次 skill，每次 60ms 进程启动，约 0.2–0.4s 纯开销）；
- 需要独立发布 skill、不想每次重建 Python 镜像；
- 多语言客户端共用这套引擎。

那时把 `runner.js` 包成常驻 HTTP 服务即可，`SkillBridge` 的接口形状不用变（内部换成 `httpx.post`）。

## 8. 已知风险

- `apt-get install nodejs` 装的是 Debian 自带版本（bookworm 是 18.x）；skill 只用了基础 API，
  但升级到新语法前要确认 node 版本；
- 如果 skill 以后加了 npm 依赖，vendor 脚本要补 `npm ci --omit=dev`；
- 每次调用起子进程，超时/僵尸进程由 `subprocess.run(timeout=)` 保证，但极端情况下仍建议加
  进程数上限（当前单次请求最多 6 次调用，风险低）；
- skill 的 `meta.skill_version` 现在是 `unversioned`，建议让 skill 侧写入语义化版本，
  线上排查会更容易。
