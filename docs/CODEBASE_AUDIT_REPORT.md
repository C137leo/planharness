# 代码库与文档审计

审计日期：2026-11-14 至 2026-11-15。基线分支 `feature/make_project_more_like_agent`，原 HEAD `ed3bbef024613a219a96771e8e87fff9b124ac9b`。依据当前工作区源码、配置、路由/能力与引用，不依据旧报告的完成声明。

## 当前架构与审计范围

当前架构、运行链和接口分别收敛到 [ARCHITECTURE](ARCHITECTURE.md)、[AGENT_RUNTIME](AGENT_RUNTIME.md)、[API_REFERENCE](API_REFERENCE.md)。统一聊天、14项原本地能力与24项其他边界能力分离于独立Graph/知识/旧评测接口。当前扫描得到104个API方法/路径、38个可派发能力定义；模型实际菜单按上下文/配置过滤。

递归包括项目隐藏目录；排除node_modules、.git对象、venv、Python/构建缓存、dist/build与vendor依赖目录。Markdown全清单 [markdown-inventory.json](audit/markdown-inventory.json) 按scan_phase区分初始96份和本轮新增7份，保存每份文档路径、主题、用途、模块、准确性、引用、价值、处理与证据、原始hash及跟踪状态。源码静态引用见 [source-references](audit/source-references.json)。本次是保守结构/入口审计和确定安全的文档/生成物清理，不声称逐行安全证明全部业务逻辑。

## Markdown 文档审计结果

初始扫描 **96** 份（md/mdx）；分类按处理前状态计，更新后不篡改原分类：

| 分类 | 数量 | 含义 |
|---|---:|---|
| MD-A | 4 | 当前必要的部署/来源/评测规范 |
| MD-B | 2 | README与检索说明部分过时，更新 |
| MD-C | 14 | 重复架构/运行/部署说明，合并后删除原文件 |
| MD-D | 20 | 阶段报告/历史证据；15份移除，5份原始历史证据保留 |
| MD-E | 0 | 本次未另以“无价值”认定未知资料 |
| MD-F | 56 | 参考、学习、规划和决策用途未充分确认，全部保留 |

计划并核实的删除 **29** 份，其中合并 **14** 份，历史阶段材料 **15** 份。新增权威/维护文档 **7** 份，实际更新既有文档 **24** 份（其中参考边界标注并非逐篇重新验证全部原文）；最终 **74** 份，比初始净减少22份。统计与路径见 [summary.json](audit/summary.json)。更新后的当前核实/维护文档共13份（原MD-A/B共6份，加新增7份），其余保留文档不冒充当前技术说明。没有为了减少数量而删学习笔记、产品规划、重要决策或安全规范。保留编号参考资料增加醒目边界说明，原内容不充作当前事实。

README去除“无聊天/无真实点餐”、旧排期和旧Console定位；新AGENTS明确源码优先、当前入口、禁止默认生成任务报告、保护未知资料/现有工作和隔离测试。部署说明去除“今晚22点/明天”时间性指令，明确现有角色密码与配置生成顺序，保留非root、备份、回滚和安全约束。

## 删除清单与恢复

逐项明细、原用途、原因、替代文档、hash及恢复commit见 [deletions.json](audit/deletions.json)。每份原文删除前已逐字节核对恢复源；已跟踪文档可从原HEAD恢复，原未跟踪报告可从既有 `refs/codex-checkpoints/console-runtime-after-20261114T140508Z` 恢复。另在本机 `/tmp/planharness-audit/docs-before.tar.gz` 保留原96份文档快照（不提交，不能当长期唯一备份）。

| 原文件路径 | 原用途 | 删除原因 | 替代文档 | Git提交 |
|---|---|---|---|---|
| `docs/02-ux-wireframes.md` | # 02 页面线框; ## 0. 设计原则 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/03-domain-and-protocol.md` | # 03 领域模型与协议; ## 1. 设计原则 | 重复说明已按当前源码合并 | `docs/ARCHITECTURE.md` | 2876761 |
| `docs/05-one-week-sprint.md` | # 05 一周冲刺（裁剪版 / 回退方案）; ## 0. 结论 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/06-full-scope-sprint.md` | # 06 全量 P0 一周冲刺（2026-10-31 ～ 11-06）; ## 环境状态（2026-10-31 14:50） | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/07-environment-and-deploy.md` | # 07 环境与部署; ## 1. 当前环境 | 重复说明已按当前源码合并 | `docs/DEVELOPMENT.md` | 2876761 |
| `docs/10-code-map-and-flows.md` | # 10 代码地图与流程图; ## 一、一句话说清这个项目 | 重复说明已按当前源码合并 | `docs/ARCHITECTURE.md` | 2876761 |
| `docs/11-progress-review.md` | # 11 进度复盘：做了什么、链路图、LangGraph 缺口; ## 1. 一句话总结 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/13-langgraph-integration.md` | # 13 接入 LangGraph：把补丁函数包成节点; ## 0. 先看注册改动 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/16-graph-wiring-template.md` | # 16 让 API 真正走 LangGraph：照着填的模板; ## 0. 完成标准 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/17-run-lifecycle-and-verifier.md` | # 17 下一阶段模板：AgentRun 生命周期 + 独立 Verifier; ## 0. 为什么先做这两个 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/18-remaining-work.md` | # 18 剩余工作清单（2026-11-03）; ## 0. 当前快照 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/19-final-priorities.md` | # 19 当前阶段与剩余优先级（2026-11-04）; ## 0. 一句话 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/CODEBASE_AUDIT_REPORT.md` | 2876761 |
| `docs/20-flexible-patch-flow.md` | # 20 一句话改计划：从「固定补丁」到「可协商草案」; ## 1. 原来的问题 | 重复说明已按当前源码合并 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/20-food-ordering-and-rag.md` | # 20 订餐 + 健康知识 RAG：落地方案; ## 0. 先定两条边界 | 重复说明已按当前源码合并 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/21-agentic-today-planner.md` | # 21 今日规划 Agent：让模型自己决定调哪些工具; ## 0. 先分清：什么叫“不是 workflow” | 重复说明已按当前源码合并 | `docs/ARCHITECTURE.md` | 2876761 |
| `docs/21-skill-driven-plan.md` | # 21 把教练 skill 接进产品：生成 → 确认 → 执行; ## 1. 责任划分（先定这个，后面都是细节） | 重复说明已按当前源码合并 | `docs/ARCHITECTURE.md` | 2876761 |
| `docs/24-mcd-order.md` | # 麦当劳 MCP：从"推荐"走到"停在付款前"; ## 1. 为什么网页购物车里没有 Agent 的餐食 | 重复说明已按当前源码合并 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/30-observability-and-targets.md` | # 可观测性（trace / token / 成本）与营养目标口径; ## 1. trace 里大量 `{}` 是怎么来的 | 重复说明已按当前源码合并 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/32-deploy-skill.md` | # 线上部署：skill（healthy-fitness-coach）怎么跟着走; ## 1. 先看它是什么 | 重复说明已按当前源码合并 | `docs/DEVELOPMENT.md` | 2876761 |
| `docs/38-unified-coach-agent.md` | # 38 统一健身教练 Agent：入口、记忆与训练执行; ## 一、一个入口，四种上下文 | 重复说明已按当前源码合并 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/39-memory-lifecycle-and-consistency.md` | # 39 记忆生命周期、临时上下文 TTL 与计划展示一致性; ## 一、当前数据结构（修改后） | 重复说明已按当前源码合并 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/40-global-coach-workspace.md` | # 40 GlobalCoachWorkspace：全站统一教练工作区; ## 一、组件与状态 | 重复说明已按当前源码合并 | `docs/ARCHITECTURE.md` | 2876761 |
| `docs/CONSOLE_RUNTIME_ARCHITECTURE.md` | # Coach Agent 运行工作台：当前实现与边界; ## 实际调用链 | 重复说明已按当前源码合并 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/coach_agent_architecture_refactor.md` | # Coach Agent 架构审查与执行闭环重构; ## 1. 当前架构（重构前的源码事实） | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/coach_agent_capability_migration.md` | # Coach Agent 能力迁移（持续更新）; ## 阶段与证据规则 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/coach_agent_full_refactor_report.md` | # AI Coach 自然语言交互与执行系统完整重构报告; ## 1. 原始架构与真实调用链 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/coach_agent_mixed_confirmation_fix.md` | # 混合确认、对象角色绑定与输入框修复; ## 根因与修复 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/coach_agent_semantic_binding_refactor.md` | # Coach Agent 语义绑定重构实录; ## 1. 调查时的架构与五个根本问题 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/AGENT_RUNTIME.md` | 2876761 |
| `docs/coach_agent_semantic_rule_inventory.md` | # 语义规则审计位置索引 | 阶段计划/报告已失效，保留重要边界与原证据 | `docs/AGENT_RUNTIME.md` | 2876761 |

## 代码、资源与依赖

文件扫描清单 **1380** 项（含另行补查的42项Git跟踪生成物）：A=296、B=878、C=128、D=29、E=49，详见 [file-inventory.json](audit/file-inventory.json)。分类用于处理，不代表每个分支都经运行验证；D项29份均为本次已核实的文档；C项保留、无业务源码删除。旧语义模块仍有legacy测试/CLI引用；独立Graph、计划与知识API仍有运行入口，均保留。

静态资源 **165** 项（含本地Swagger的3项JS/CSS；A=19、B=76、C=70、D=0）；逐文件路径、hash、精确引用和保守分类见 [asset-inventory.json](audit/asset-inventory.json)。共6组完全相同hash文件，均为独立历史截图/评审记录，未删除。13张动作缩略图由thumbs.ts动态映射保留。没有删业务图片、用户资源或数据库。

42项Git跟踪H5产物确认可通过uni build再生，构建验证后仅移除Git跟踪，保留本地磁盘；详见 [generated-files.json](audit/generated-files.json)。扩展ignore为整个frontend/dist并忽略本地.idea，避免生成结果污染提交。没有粗暴清缓存目录。

声明依赖22项（含httpx运行/开发双用途）均保留：[dependencies.json](audit/dependencies.json)。LangGraph、Postgres checkpointer、psycopg驱动/连接池、jsonschema MCP校验与uni平台插件均有独立运行或工具链用途；不以缺少直接import删插件，不升级依赖或改锁文件。Dockerfile与models.py只修正已删除文档的注释引用（models只暂存该注释片段）；.env.example只修正“无模型时统一聊天也有模板兜底”的错误注释。

配置补查 [configuration.json](audit/configuration.json) 记录3个公开环境模板中的61项键声明（含注释/跨模板重复，35个不同键），均找到Settings、桥接、前端或部署读取依据。只记录键名和引用，不收集真实环境值；POSTGRES_PASSWORD仍用于compose替代入口，因此保留。

## Git安全与提交边界

执行前记录工作区状态/空index于 `/tmp/planharness-audit/`。原工作区包含大量此前未提交的业务代码、测试、评测和UI实现；已存在专用恢复检查点，不能把这些改动当垃圾或混入本次文档提交。本轮只暂存核实过的文档、机器清单、ignore、Docker注释和生成物取消跟踪；不推送，不reset/clean，不提交环境凭证/业务数据/用户资料。

已创建正常本地提交（未推送）：

- `2876761f3c41b4a89357529ef46c917102d811e4`：权威文档、29份原文清理及清单、维护规则和引用修复。其中原已跟踪删除22份，原未跟踪报告7份通过专用检查点恢复，不能误称7份也在本提交有删除diff。
- `469381bf1ebfc362b19d1d614456c083f403cb50`：42项构建产物取消跟踪、dist/IDE忽略规则；本地42项文件hash保持一致。
- 本报告及JSON中的实际提交关联在随后独立的provenance提交中补齐；可用 `git log -- docs/CODEBASE_AUDIT_REPORT.md` 定位该提交。

[Git整理记录](audit/git-cleanup.json)保存基线与剩余状态计数、恢复hash核验和提交范围。清理后工作区仍保留原有业务修改；不将“全部工作区清空”作为审计成功条件。

## 验证记录

本轮仅执行选定边界/工具链检查，未重跑后端全套旧测试：

| 检查 | 实际命令 / 范围 | 结果 |
|---|---|---|
| Markdown本地链接 | `python backend/scripts/check_doc_links.py` | 74份，0缺失；初始96份也是0缺失；不检查远程URL和标题锚点 |
| 运行代码文档引用 | 扫描253个Python/部署脚本/配置入口中的删除路径 | 修复Docker与models文档注释；0残留 |
| 后端边界 | `cd backend && .venv/bin/python -m pytest tests/test_runtime_console.py tests/test_capability_migration_contract.py tests/test_auth.py tests/test_sqlite_config.py -q` | 30通过，13条第三方弃用告警；临时SQLite、Scripted模型，不联网 |
| 前端 | `cd frontend && node --test tests/*.test.cjs` | 56通过 |
| H5 | `UNI_OUTPUT_DIR=/tmp/planharness-audit/h5 node node_modules/@dcloudio/vite-plugin-uni/bin/uni.js build` | 通过；背景图编译期提示保留运行时路径，已确认源码和产物都有static/bg.jpg |
| 微信构建 | `UNI_OUTPUT_DIR=/tmp/planharness-audit/mp-weixin node node_modules/@dcloudio/vite-plugin-uni/bin/uni.js build -p mp-weixin` | 通过，非真机验证 |
| 检查器自身 | 临时目录中的隐藏目录/忽略依赖、失效链接、参考式链接3项CLI检查 | 通过 |
| 差异格式 | `git diff --check` 与暂存差异检查 | 通过 |

构建/前端测试使用本机已安装Node22可执行文件。未新增独立严格TypeScript类型检查承诺（项目未提供对应脚本），未调用真实模型、官方MCP、支付或生产数据库。完整原始本轮日志保留于本机 `/tmp/planharness-audit/`，不把临时日志和认证数据提交。

## 遗留事项与边界

- P0：现有能力/工作台缺少完整独立整体目标验证、所有工具/外部操作的提交回滚证据；遵循unknown核实机制，不能用success字符串证明全部目标。
- P1：现有业务改动尚未整理为正式提交；发布前需单独review，并验证Postgres加法迁移、并发/中断与生产凭证边界。此次不将这些未完成变更混入清理提交。
- P1：旧Graph/独立API/legacy兼容需明确退役政策和外部调用窗口，当前不删除。来源不明/用户独立资料见 [UNUSED_CODE_REVIEW](UNUSED_CODE_REVIEW.md)。
- P2：工作台实时事件/崩溃持久化、长历史性能、全局目标证据、更强脱敏/保留周期和真机验证仍待后续任务，不借本次清理重构业务。

本次本地测试不替代真实模型语言准确率、官方MCP供应状态、支付、生产部署或微信真机验收。
