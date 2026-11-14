# 开发、测试与维护

## 环境与启动

Python >=3.12、Node >=22.12。后端依赖/锁文件为 backend/pyproject.toml、uv.lock；前端为 frontend/package.json、package-lock.json。不要混用锁文件或无关升级。

```bash
# 仓库根目录
cd backend
uv sync --group dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

```bash
# 另一个终端，仓库根目录
cd frontend
npm ci
npm run dev:h5
```

配置见仓库根目录的 [后端模板](../.env.example) 和 config.py。首次本地配置可从 backend 目录执行 `cp ../.env.example .env`（已有配置先保留，不覆盖）。环境变量优先于启动目录的 .env；生产使用 systemd EnvironmentFile，凭证不入库。前端开发代理 5173 → 8000，生产模板 API 9200，二者不要混淆。

## Skill 与独立计划路径

skill/bridge.py 通过 runner.js 启动 Node 子进程。SKILL_DIR 必须指向包含 agent-integration/coach.js 的实际引擎目录，SKILL_NODE 可指定 Node，SKILL_TIMEOUT 控制单次超时。开发机默认路径不是部署契约；部署用 deploy/vendor-skill.sh 准备 vendor 内容并显式配置。桥接只传环境白名单，不继承模型密钥。

Skill 缺失时桥接返回 unavailable，上层部分独立计划接口有保守降级；不能据此声称模型聊天有规则 fallback。健康检查会报告 skill 状态，发布前核对实际生成路径。

## 增量检查

```bash
cd backend
uv run pytest tests/test_runtime_console.py tests/test_capability_migration_contract.py
```

根据改动选择相关测试文件/用例，旧流程测试集中在 tests/legacy。后端 conftest 在导入应用前覆盖临时 DB/空模型密钥；单独脚本必须自己配置隔离。不要直接导入默认 app.main 对用户数据库做“只读检查”，初始化会写库。

```bash
cd frontend
node --test tests/*.test.cjs
npm run build:h5
npm run build:mp-weixin
```

build:h5 默认输出 dist/build/h5，mp-weixin 输出 dist/build/mp-weixin；均为生成文件，不入 Git。可设置 UNI_OUTPUT_DIR 指向临时目录以保护现有产物。当前 package.json 未提供独立 typecheck 脚本；构建通过不等于严格类型检查或微信真机验收。

browser.cjs 是带独立服务/Chrome fixture 的人工集成入口，不和 *.test.cjs 混跑。真实模型评测和商务测试需要凭证且可能产生费用或外部状态，本地普通检查使用 Scripted 模型/假 MCP。历史 holdout 的退役/保密状态见 [语料说明](../backend/app/evals/dialogue_suites/README.md)，不将已揭示样本称为未见样本。

## 发布与文档维护

从仓库根目录执行 `python backend/scripts/check_doc_links.py` 检查全部项目 Markdown 的本地文件链接（不检查远程URL或标题锚点），包含自有隐藏目录并排除依赖/缓存/构建目录。

systemd/Nginx/PG 的备份、权限、数据库角色、checkpointer、回滚步骤集中在 [部署说明](../deploy/README.md)。本地检查不代替生产迁移验收；启动期加法迁移异常需单独查日志。不要运行默认 compose 覆盖已有部署。

优先更新权威文档，不为每次开发另写总结报告。删除须核实真实入口、字符串注册、脚本读取和动态图片映射；不确定则记录 [待确认项](UNUSED_CODE_REVIEW.md)，保留原路径。维护规范见 [AGENTS.md](../AGENTS.md)。
