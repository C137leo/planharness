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

计划页日历、周模板与教练草案预览使用用户本地日期，从今天起连续 7 天；`day_index` 仍按周一=1、周日=7 匹配模板。饮食按日展示响应式卡片。教练面板可拖动高度（键盘方向键/Home/End 亦可调整），内嵌与弹窗分别缓存高度，窗口变化时重新限制到可用视口内。相关回归：`cd frontend && node --test tests/plan-window-resize.test.cjs`。

## 增量检查

动作库跳转恢复自动流程：先完成剪贴板复制（同步复制不可用时使用 Clipboard API/uni 回退），然后将提示直接挂在页面 body 最前景，停留 1 秒后自动打开动作库；新窗口受拦截时在当前标签导航。没有二次点击确认；复制失败明确提示手动搜索，不声称成功。`tests/exercise-guidance.test.cjs` 验证复制先后、计时和置顶层级。

中文动作指导维护于 `backend/app/domain/exercise_guidance_zh.json`（57 项上游指导的中文整理）；生成审计目录时写入 `instructions_zh`，英文原文与具体来源保留供溯源。知识种子启动时更新已存在条目，界面不再显示上游英文正文。计划页与训练页共用 `KnowledgeSheet`，外部动作库入口使用上述复制、置顶提示和自动跳转流程。动态热身与全身拉伸新增插图位于 `frontend/src/static/ex/generated-v2/dynamic_warmup.jpg`、`full_body_stretch.jpg`；由内置 imagegen 生成，提示词为桃奶油淡彩、棕色细线、白背心棕短裤男性、淡城市远景，分别展示温和站姿抬膝热身及站姿侧伸展。

训练页操作分为当前组记录、四项训练辅助、本场会话操作三层；桌面端辅助按钮四列，窄屏两列。只有记录当前组使用实心主按钮，结束反馈使用独立弱强调按钮；请求处理中按钮禁用，原有记录、跳过、暂停、替换与反馈接口保持不变。

Console 顶部的大模型总开关仅向 `/auth/me` 返回 `is_llm_admin=true` 的账号展示；默认运营手机号为 `13772692821`（配置项 `LLM_ADMIN_PHONE`）。读取与修改仍由 `/admin/llm-switch` 在后端校验权限。暂停影响全站模型调用，不影响登录与已保存内容读取；进入 Console 时读取实际状态，支持暂停、恢复和失败后刷新。

公开演示登录由 `DEMO_LOGIN_ENABLED` 控制，默认 `false`，生产保持关闭。登录页仅在 `/auth/options` 明确返回 `demo_login_enabled=true` 时展示入口；关闭时 `/auth/demo` 返回 404。开启后请求体也只能是 `{}`，传 `user_id` 等额外字段返回 422，只签发服务端固定 `DEMO_USER_ID` 的令牌。种子数据继续保留；共享测试 fixture 显式开启演示登录。关闭入口不会撤销已签发令牌，也不会改变正常手机号登录、大模型管理权限或暂停状态。

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

提交时纳入源码、测试、评测样本、配置模板、锁文件及运行所需静态资源。`docs/evidence/` 与 `backend/eval_intent_result.json` 是本地生成的验收输出，保留磁盘并忽略，不把原始模型轨迹、认证响应和大批截图加入 Git。历史数据不是当前通过率。测试实际读取的两份稳定输入位于 `backend/tests/fixtures/`：迁移前能力菜单用于契约比较，官方营养只读样本用于隔离 UI fixture；修改原始报告不应改变这些固定测试输入。

## PostgreSQL 字段兼容检查

启动时 ensure_additive_schema 会调用 ensure_postgres_text_capacity，将旧库中短于当前ORM契约的VARCHAR扩宽；模型为Text而实际仍为VARCHAR的列转为TEXT。不缩短、不截断，不删除数据。失败时PostgreSQL启动终止，避免带错误结构运行。部署前备份，并确保迁移账号具有ALTER权限。本地SQLite不能验证VARCHAR长度，需要真实PostgreSQL回归：

```sh
PLANHARNESS_TEST_POSTGRES_URL=postgresql+psycopg://... pytest tests/test_postgres_capacity.py
```

该测试只使用显式提供的测试URL，在随机临时schema中建表并清理，不应填写生产地址。截图对应的旧task_id(32)、goal(32)及快照revision(32)已在隔离PostgreSQL16复现并验证扩容、旧数据保留及幂等。本次未修改生产数据库。

动作指导的少量已核验补充维护在 `backend/app/domain/exercise_guidance.json`，由知识库种子和前端 helper 共用。计划页优先展示这些本地指导；山羊挺身可继续复制准确英文名称到 free-exercise-db 搜索。靠墙静蹲不在当前本地上游 raw 中，使用独立机构来源，不映射为普通深蹲。被忽略的 raw 数据不是运行依赖；更改来源时同步检查知识来源白名单。

动作插图由 `frontend/src/styles/exerciseImages.json` 按动作 ID 精确映射；新增生成图放在 `frontend/src/static/ex/generated-v2/`，执行 `python3 backend/scripts/build_exercise_image_manifest.py --check-complete` 重建清单并检查目录覆盖，只引用实际存在的图片。靠墙静蹲与山羊挺身使用独立的 `*-v2.jpg`，目录外未知动作显示文字占位，不按身体部位冒用其他动作图。轻食图放在 `frontend/src/static/meals/`，`mealThumb` 按早餐、午餐、晚餐（兼容 `meal_` 前缀）选择分类示意图，不表示具体材料或份量。插图使用内置 imagegen，基础提示词为桃色奶油色淡彩、细棕线、淡城市背景、方形无字缩略图；动作逐项指定姿势与器械，三餐分别为燕麦鸡蛋水果、蛋白蔬菜谷物碗、豆腐白鱼蔬菜盘。

在 macOS 上可用 `bash backend/scripts/export_generated_thumbnails.sh`（需要系统 `sips`）导出 384px 网页 JPEG，并将原始 PNG 归档到被 Git 忽略的 `docs/assets/exercise-sources/`；原图不随源码上传，发布使用压缩后的前端静态资源。此步骤保留原图，不覆盖已有归档。

302项指导覆盖审计由 `backend/scripts/build_exercise_guidance.py` 从本地忽略 raw 生成 `exercise_guidance_catalog.json`，仅唯一英文名精确匹配与显式审核同义词；当前58项可用（含两项中文补充），未匹配项不冒用子串搜索。上游匹配项保留完整英文 instructions 与具体出处，运行不读取 raw。首份草案在人工模式候选中优先选择有指导、器械可用、安全且非排除的动作；用户明确选定动作和已有草案修订不受此优先策略影响。
