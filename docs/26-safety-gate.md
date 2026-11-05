# 安全门（Safety Gate）：谁在判断、凭什么判断、漏了怎么办

## 0. 一句话

**安全门是一组确定性代码规则，独立于模型运行；模型只能"提案"，门决定能不能落库、能不能提交。**
判断依据不是"猜这段话什么意思"，而是**结构化数据**（用户档案、动作目录元数据、计划结构）
＋ **少量关键词表**（只用于"红旗症状"这种宁可误伤的场合）。

## 1. 先纠正一个误解

> "安全门就是关键字过滤，命中就过滤掉/不予生成"

只对了一小半。项目的门按**位置**分成五道，只有第一道是纯关键词：

| # | 门 | 位置 | 判断依据 | 命中后果 |
|---|---|---|---|---|
| 1 | 红旗症状筛查 | 输入（问句/指令） | `RED_FLAGS` 关键词表（`knowledge/service.py:14`） | **不生成计划**，改回就医话术 |
| 2 | 目标安全门 | 算目标时 | 热量下限、缺口 ≤25%、蛋白 1.2–2.2 g/kg、训练 2–5 天（`domain/planning.py:103`） | 抛错，不出计划 |
| 3 | 生成侧本地门 | 计划草稿 | 训练天数 ≤ 用户每周可练、忌口/过敏原是否出现在餐里（`agent/planning_agent.py:680`） | 打回/加 caution |
| 4 | 补丁安全门 | 改计划 | 动作目录 `knee_risky` / `movement_mode`、`FORBIDDEN_KNEE_REFS`、力量训练日数（`domain/patches.py:196`） | **422 拒绝补丁**（不静默改） |
| 5 | 输出校验 | 今日规划结果 | 每餐 3 选项、忌口、链接白名单、引用必须真实存在、无用药建议（`agent/today_agent.py:356`） | 结果不落地 → 降级到确定性编排 |

另外两道是**非代码的兜底**：人工审批（必须点确认）、审批时**重跑**第 4 道门。

## 2. 代码：补丁安全门（最核心的那道）

```python
# backend/app/domain/patches.py:43
FORBIDDEN_KNEE_REFS = {"squat", "goblet_squat", "run", "jump", "lunge"}

# backend/app/domain/patches.py:179
def is_knee_risky(item: dict) -> bool:
    raw_ref = str(item.get("action_ref") or "")
    ref = raw_ref.replace("-", "_").lower()          # goblet-squat 和 goblet_squat 归一
    if ref in FORBIDDEN_KNEE_REFS:
        return True
    if any(keyword in ref for keyword in KNEE_RISK_KEYWORDS):
        return True                                   # 关键词兜底：squat/lunge/run/jump...
    from app.domain.exercise_catalog import KNEE_RISKY_SLUGS
    if raw_ref in KNEE_RISKY_SLUGS:
        return True                                   # 目录里标注过的风险动作
    meta = item.get("meta") or {}
    return str(meta.get("movement_mode") or "") in KNEE_RISK_MOVEMENTS  # knee_dominant
```

```python
# backend/app/domain/patches.py:196
def _safety_check(proposed, available_days, *, check_knee=True) -> list[str]:
    errors = []
    if check_knee:                       # 只在"膝痛/换部位"类补丁里开
        for item in proposed:
            if item["action_type"] != "exercise":
                continue
            if is_knee_risky(item):
                name = (item.get("meta") or {}).get("name_zh") or item["action_ref"]
                errors.append(f"膝痛禁忌动作: {name}")
    strength_days = {item["day_index"] for item in proposed
                     if item["action_type"] == "exercise" and item["slot"] == "strength"}
    if len(strength_days) > available_days:
        errors.append(f"力量训练日 {len(strength_days)} 超过可用 {available_days} 天")
    return errors
```

注意 `check_knee` 的**作用域**：「今天想休息」这类补丁本来就会保留部分原动作，
用膝痛禁忌去卡它会把合理补丁 422 掉，所以按 `intent.kind` 开关（`injury` / `injury_swap` 才开）。

## 3. 判定信息从哪来

| 来源 | 具体内容 | 谁维护 |
|---|---|---|
| 用户档案 | 过敏原、忌口、每周可练天数、身高体重目标 | 用户填 + `resolve_tags` 解析 |
| 动作目录 | `knee_risky` 布尔、`movement_mode`（knee_dominant 等）、中文名 | `domain/exercise_catalog.py`（从动作库同步） |
| 代码常量表 | 膝痛禁忌 ref、红旗症状词、医疗词 | 人写，进代码评审 |
| 计划结构 | `slot` / `day_index` / `action_type` / sets / duration | 生成器产出 |
| 意图解析 | `available_days`、`body_parts`、`kind` | 一句话 → intent |
| 官方知识库 | 红旗症状词对应的就医说明（引用来源） | `knowledge/seeds.py` |

## 4. 命中关键词 ≠ 过滤显示

三种命中、三种处理，**都不是"把字藏起来"**：

| 命中 | 处理 | 用户看到 |
|---|---|---|
| 红旗症状（胸痛/晕厥…） | 不生成计划 | "检测到红旗症状，今天不做训练规划" + 就医建议 + 来源 |
| 膝痛禁忌动作 | **422 拒绝补丁**，并把动作中文名列出来 | "膝痛禁忌动作: 高脚杯深蹲" |
| 忌口/过敏原 | 生成时排除；输出再校验一次，泄漏就降级 | "出现忌口食材 虾" |

不给"悄悄删掉一条建议就当作解决了"的选项——**拒绝并说明原因**是可审计的，
静默修改会让用户以为计划本来就长这样。

## 5. 关键词/正则覆盖不全怎么办（这是这道题的重点）

1. **能不用文本就不用文本**：动作风险的主要依据是目录元数据（`knee_risky` / `movement_mode`），
   不是动作名字。名字只做兜底，且做了 `-`/`_` 归一 + 关键词双写。
2. **未识别就明说，不默认安全**：忌口解析不了 → 往 `cautions` 里写"这些忌口不在食谱库里，
   请人工确认"（`planning_agent.py` 的 `unmatched` 分支），而不是当没填。
3. **代价不对称时选 fail-closed**：红旗症状宁可误伤（让人去就医），也不放过；
   相反，"今天想休息"这种场景用膝痛门去卡会误杀合理补丁，所以按 intent 收窄作用域。
   安全门不是"越严越好"，而是"错的代价 × 概率"加权。
4. **人工审批是最后一道**：模型和规则都放行的东西，仍然要人点确认；
   审批时**重跑**安全门，防止草稿期与审批期之间状态漂移（`approve_patch` 里再调一次 `_safety_check`）。
5. **回归测试锁门**：`tests/test_safety_gate.py` 22 个用例把"该拦的拦、不该拦的别拦"钉死；
   改松规则会立刻红。
6. **eval 场景兜底**：E01 红旗必须阻断、E02 膝痛补丁 churn=25% 且审批前不写库。

**已知缺口（要主动说）**：
- 完全没登记过的动作名不会因为"名字不像"被拦（`is_knee_risky` 返回 False）；
  兜底靠目录 + 人工审批，不靠关键词穷举；
- 红旗词表是人工维护的中文短语，口语变体（"心脏那位置闷闷的"）可能漏 → 所以还有
  "不构成医疗建议 + 不适即停"的产品级兜底，并且知识问答侧也做了同一套拦截；
- 目前没有"模型语义识别红旗"的兜底链路（那会把判断权交回给模型，代价是漂移）。

## 6. 在 Console 里怎么体现

打开 Console 选一条运行，`SAFETY · 安全门` 卡会显示：

- **红旗阻断**：`拦截 · 作用域 red_flag · ✗ 检测到红旗症状，今天不做训练规划。`
  （对应运行状态是 `rejected`，trace 里只有 `agent → safety_screen → validate`）
- **补丁通过**：`通过 · 作用域 rest · 审批时重跑安全门：已通过`
  + "确定性规则全部通过：没有膝痛禁忌动作、训练日不超过可用天数、忌口与链接白名单无冲突"
- **补丁被拒**：运行不会产生（接口直接 422），Console 里看到的是拒绝原因文本

配套的三块：`DIFF · 最小补丁`（改了几处）、`TRACE`（`safety_check` 这一步）、
`AUDIT CHAIN`（`approval_granted` / `patch_committed`）。

> 这张卡是这次补的：之前 `/runs/{id}` 只回传 diff 和 operations，
> 不返回 `patch.validation`，所以 Console 看不到安全门的结论。

## 7. 面试怎么讲（30 秒）

> 安全门是**模型之外的确定性闸口**，模型只能提案。判断依据是结构化数据：动作目录里标记的
> 膝痛风险、`movement_mode`、用户档案里的忌口和可练天数、计划结构里的槽位和天数——
> 关键词表只在"红旗症状"这种宁可误伤的场合用。
> 命中不是"藏起来"，而是**拒绝并说明原因**：红旗直接不生成计划，膝痛禁忌 422 拒绝补丁。
> 兜底有三层：未识别的忌口显式提示人工确认、人工审批必须点确认且审批时重跑安全门、
> 22 个单测 + eval 场景把"该拦的拦、不该拦的别拦"钉住。

## 8. 追问与答法

**Q：为什么安全判断不交给大模型？**
A：模型不可审计、会漂移，且它同时是"提案者"——让提案者给自己发许可证是结构性错误。
模型的产出必须能被一条确定性规则否决，否则出了事故无法回答"当时谁放行的"。

**Q：关键词表一定漏，你怎么办？**
A：所以主判定不靠关键词。动作风险看目录元数据 + 结构；忌口解析不了就明说"请人工确认"；
红旗词表漏的情况由产品级兜底（不适即停 + 免责声明）和人工审批补位。关键词只做加法，不做唯一依据。

**Q：安全门会不会把合理计划也拦掉？**
A：会，所以要**按作用域收窄**：膝痛门只在 `injury`/`injury_swap` 意图里开，
「今天想休息」这类补丁跳过膝痛检查；这条是踩过坑之后加的（之前休息补丁被 422 拦过）。

**Q：怎么证明这套东西没被改松？**
A：`tests/test_safety_gate.py` 覆盖"两种 slug 写法都要拦""目录标注要拦""休息补丁不该被拦"
"力量日超限要拦"；改规则就红。再加 eval 场景断言红旗必须阻断。

## 9. 代码索引

| 文件 | 内容 |
|---|---|
| `backend/app/domain/patches.py:43,179,196` | 膝痛禁忌集、`is_knee_risky`、`_safety_check` |
| `backend/app/domain/exercise_catalog.py:314` | `KNEE_RISKY_SLUGS`（目录元数据） |
| `backend/app/domain/planning.py:103` | `validate_targets`（热量/蛋白/天数） |
| `backend/app/agent/planning_agent.py:680` | `_local_gate`（天数、忌口、未识别忌口） |
| `backend/app/knowledge/service.py:14` | `RED_FLAGS` / `MEDICAL` |
| `backend/app/agent/today_tools.py:143` | `safety_screen` 工具（模型也必须调） |
| `backend/app/agent/today_agent.py:356` | `validate()` 输出校验 |
| `backend/app/api/routes.py` | 审批时重跑安全门、422 返回 detail |
| `backend/tests/test_safety_gate.py` | 22 个安全门用例 |
