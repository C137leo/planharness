# HITL 与版本/审计：人工审批到底审什么，版本链到底解决什么

## 0. 先纠正两个直觉

**直觉一：「审批 = 确认要不要重新生成」。**
不是。生成和提交是两件事：

```
一句话 → 解析意图 → 生成受控操作 → 算 diff/安全门 → 落"草案"（pending_approval）
                                                          │
                                      计划版本、动作、餐食 —— 一个字节都没变
                                                          │
                                              人工点"确认应用"
                                                          ↓
                                    重跑安全门 → 写新版本 → 审计链记一笔
```

关键是**审批前不落库**：`create_request` 只写 `plan_patches`（草案）和 `patch_requests`，
不碰 `plan_versions` / `plan_actions` / `meal_plans`。
不信可以看：草案阶段 `GET /plans/current` 的 `version` 不变，审批后才 +1。

**直觉二：「版本没什么用，回退也看不到是哪里」。**
以前确实没有"版本列表"这个界面，只有一句"撤销会新建版本"，你自然看不出它解决了什么。
这次补了 `/plans/{id}/versions` + Plan 页的「版本记录」+ Console 的 `VERSIONS` 卡，
每条版本有：`v` / `parent_version` / 相对上一版改了哪些槽位、几餐 / 来源补丁 / 状态。

## 1. HITL（Human-in-the-loop）在本项目的四个体现

| # | 体现 | 代码位置 | 关键点 |
|---|---|---|---|
| 1 | 模型只能提案 | `agent/change_agent.py` / `agent/patch_graph.py` | 模型输出受控操作，不能直接写库 |
| 2 | 草案与提交分离 | `domain/requests.create_request` → `approve_patch` | 审批前 `plan.version` 不变 |
| 3 | 审批页的"物理"防误触 | `pages/approval/index.vue` | 进页面 800ms 冷却 + 必须勾选 + 显式按钮 |
| 4 | 审批时重跑安全门 | `domain/patches.approve_patch` | 防"草稿期→审批期"之间状态漂移 |

另外两处 HITL 味道的设计：

- **驳回也留痕**：`reject_patch` 只改状态，不动计划；审计链记 `approval_rejected`。
- **审批人是谁写进审批记录**：`Approval(actor_user_id, decision, comment)`。

**诚实地说，现在的审批有三个短板**（面试被问要主动讲）：
① 没有角色分离（用户自己批自己）；② 没有过期时间（pending 会一直挂着）；
③ 没有双人复核/额度阈值（金额或影响面大时才需要多签）。

## 2. 版本链解决什么问题

版本不是"给用户看的编号"，它解决四个工程问题：

| 问题 | 版本链的答案 |
|---|---|
| 改了之后想反悔 | 回滚 = **用旧内容再提交一个新版本**，历史不删 |
| 想比较"这次到底动了什么" | 每版都有 `parent_version`，`_diff` 算出 changed_slots / churn / meals_changed |
| 想知道"这版是谁改的、为什么" | `created_from_patch_id` → 补丁原因 + 审批记录 + 审计链 |
| 并发写入互相覆盖 | 补丁带 `base_version`，审批时比对，不一致就 `stale patch` 拒绝 |

```python
# domain/patches._new_version_from_actions
old = 当前版本; old.status = "superseded"      # 旧版本保留，只标记
new_version = PlanVersion(version=old+1, parent_version=old, status="active", ...)
# 补丁只动被点名的槽位，其余动作/餐食从 base_version 原样复制
```

回滚（`rollback_plan`）同样是"新增一个版本"：

```
v21 内容 → 回滚 → 生成 v23（内容=v21），v22 仍在历史里
```

所以"撤销"本身也能被审计、也能被再撤销，不会出现"回滚把历史抹掉"。

## 3. 审计链解决什么问题

```python
# app/audit.py
event_hash = H(stream_id, seq, action, actor, payload_digest, prev_hash)
```

每条事件把**上一条的 hash** 算进来，形成链。作用是**防篡改 + 可验证**：

- `GET /audit/{stream_id}/verify` 会从头重算，任何一条被改/被删都对不上；
- Console 的 `AUDIT CHAIN` 卡列出事件，点「验证链完整性」当场验；
- 事件类型：`patch_created` / `approval_granted` / `approval_rejected` /
  `patch_committed` / `rollback_committed` / `plan_confirmed` / `mcd_order_created` …

它和版本的分工：**版本回答"计划长什么样"，审计回答"谁在什么时候做了什么"**。

链路本身只防"改中间"；**整链重写 / 尾部截断**要靠链头签名（`sign_head`）+
把 `(seq, hash, signature)` 抄到库外。详见 `docs/29-audit-chain.md`。
两者通过 `patch_id` / `version` 串起来。

### 3.1 同一份计划只留一个待确认草案

以前连着点几次"看看会怎么改"，会堆出好几张一模一样的待确认卡片（本次修复）：
`create_request` 落新草案时会把同一计划下其它 `pending_approval` 标成 `superseded`。
理由是审批的对象必须唯一——否则用户不知道自己批的是哪一版。

### 3.2 RUNS 和 AUDIT CHAIN 不是一回事

| | RUNS | AUDIT CHAIN |
|---|---|---|
| 单位 | **一次执行**（今日规划 / 补丁审批 / 回滚） | **计划上的一个动作**（生成草案/审批/提交/回滚/下单） |
| 回答 | 这次跑了什么、trace、花了多少 token | 谁在什么时候改了计划、链是否完整 |
| 数量 | 每次调用一条 | 每个变更一条，天然比 run 多 |

Console 里两个卡都加了一句话说明，审计默认只露 12 条（`展开全部 122 条`），
事件名也翻成中文（`patch_created` → 生成改动草案）。

## 4. Console 里怎么体现（本次新增）

- `VERSIONS · 版本链`：`共 23 版 · 当前 v23`，逐条显示
  `v23 当前 来自 v22 · 内容已回滚到 v21… · 改 3/9 个槽位`；
- `DIFF · 最小补丁`：当前运行对应补丁的 churn / 改动清单；
- `AUDIT CHAIN`：事件流 + hash + 「验证链完整性」；
- `COST` / `CONTEXT`：这次运行花了多少、上下文用了多少。

Plan 页新增「版本记录」：可展开，看到每版"改了 3/9 个动作 · 3 餐 · 今天想休息"，
非当前版本可以点「回到这一版」（走同一个 rollback，生成新版本）。

## 5. 面试怎么讲（30 秒）

> 人工审批不是"确认要不要生成"，而是**确认要不要提交**：Agent 只能生成草案，
> 审批前计划版本一个字节都不变；点确认时才重跑安全门、写新版本、记审计。
> 版本链解决可回溯：每版带父版本，能算出"改了哪些槽位、几餐、churn 多少"，
> 回滚不是删历史，而是用旧内容再提交一个新版本，所以撤销本身也能被审计。
> 审计是哈希链，任何一条记录被改动都验不过去——这两条加起来，才敢说"AI 改的东西可追责"。

**追问：审批为什么不能省？** 因为模型不可审计且会漂移；
高风险写操作必须有人签收，且签收时要能一眼看到 diff（最小补丁就是为这个服务的）。
**追问：回滚会不会丢数据？** 不会，只新增版本；旧版本标 superseded，内容和餐食都还在。
**追问：怎么证明审计没被改过？** 每条事件 hash 含 prev_hash，`verify` 重算整链；
改动任何一条，后续全对不上。

## 6. 已知缺口

- 版本列表只给"改了几处/几餐"，没有逐条 action 的 before→after（补丁详情里有）；
- 审批没有角色分离、过期时间、多人复核；
- 审计流以 plan_id 为 stream，跨计划的动作（如 MCD 下单）在没计划时挂 user_id，
  两条流不合并；
- 回滚只能回滚到"已存在的版本号"，没有"撤销 N 步"的批量语义。
