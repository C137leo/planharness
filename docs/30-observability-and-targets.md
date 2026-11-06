# 可观测性（trace / token / 成本）与营养目标口径

这篇回答四个"看起来是 bug"的问题：trace 里的 `{}`、Console 右边太空、成本总是 0、蛋白质目标 176g。

## 1. trace 里大量 `{}` 是怎么来的

以前只有 `node/status/ts/detail` 四个字段，工具步骤的 `detail` 从来没填：

```python
_push(trace, on_step, name, str(result.get("status")))   # detail={}
```

现在工具步骤带两层信息：

```python
_push(trace, on_step, name, str(result.get("status")),
      args=args, brief=_brief(result))
```

- `args`：这次调用到底传了什么（`{"query": "腿举动作要领", "body_part": "knee"}`）
- `brief`：结果摘要（`{"count": 3, "citations": 3}`、`{"target_kcal": 2371.4}`、
  `{"actions_count": 3, "meals_count": 3}`）

前端顺手做了两件事：`detail` 为空对象时不渲染那行；空 `{}` 不再占位。

## 2. token / 成本一直显示 0

两个问题叠在一起：

1. 数据没暴露：`tokens_in/tokens_out` 存在 `agent_runs` 表里，但 `/runs` 的响应 schema 没有这两个字段；
2. 前端读错了地方：COST 卡读的是 `selected.output.tokens_in`，而 token 根本不在 `output` 里。

修法：

```python
# schemas.RunSummaryOut
tokens_in: int = 0
tokens_out: int = 0
cost_cny: float = 0.0

# routes._run_summary
cost = round(
    run.tokens_in / 1e6 * settings.llm_price_in_cny_per_mtok
    + run.tokens_out / 1e6 * settings.llm_price_out_cny_per_mtok, 4
)
```

价格来自配置（`LLM_PRICE_IN_CNY_PER_MTOK` / `OUT`），不写死在前端。
实测一次今日规划：`17986 in / 2487 out → ¥0.0559`。

顺带补了成本护栏：`today_plan` 之前不做预算检查，现在超过 `MAX_COST_CNY_PER_RUN`（默认 0.05 元）
会写 `output.budget.exceeded`，Console 的「预算超限」从"否"变"是"并显示原因。

补丁审批这类**不调模型**的 run 仍然显示 0 —— 那是真的 0，不是没记。

## 3. Console 两栏布局

`.console-grid` 在宽屏（≥900px）切成 `360px + 1fr`：

- 左栏：A/B/C 对比、场景回放、RUNS 执行列表
- 右栏：TRACE、DIFF、SAFETY、CONTEXT、COST、VERSIONS、AUDIT CHAIN、VERIFICATION

手机宽度自动退回单列，不改变原有信息层级。

## 4. 蛋白质 176g 是从哪来的

**来源**：营养目标不是我们算的，是 `healthy-fitness-coach` skill 给的宏量区间
（`nutrition.macros.protein_g.high_g`），它按体重 ×2.0 分配；当时档案 88kg → 176g。
`coach.confirm_draft` 原样落库，补丁/回滚又沿用基线版本，于是一直是 176g。

**你的直觉没错**：60–100g 是**一般成年人**的口径（约 0.8–1.2 g/kg，88kg → 70–106g）。
有规律力量训练 + 减脂期的运动营养口径是 **1.6–2.0 g/kg**（88kg → 141–176g）。
176g 属于这个区间的**上限**，不是错，但也没必要顶格。

**这次改了什么**：

1. `clamp_protein_band()`：把 skill 给的区间夹进 `[1.2, 2.0] g/kg`（按**当前档案体重**），
   并把 `basis_weight_kg` 记进 `plan.nutrition`；
2. 档案体重和 basis 差 ≥5% 时，`/plans/current` 返回 `nutrition.targets_stale=true` +
   `stale_hint`（"这版目标是按 88 kg 算的，你现在 65 kg…"），今日页显示提示和
   「按当前体重重算」按钮；
3. 新增 `POST /plans/{id}/retarget`：按当前档案重算热量/蛋白，**重建三餐**（用同一食物库），
   动作不动，仍生成新版本 + 审计事件 `plan_retargeted`。

实测当前计划：v23 `2360 kcal / 176 g` → retarget → v24 `2371 kcal / 140.8 g`（1.6 g/kg），
三餐重建为 720 / 738 / 482 kcal（合计 1940，仍低于目标 2371 —— 食物库按份量取整的已知偏差，
页面会直接显示"合计 / 目标"，不假装严丝合缝）。

**口径速查**

| 场景 | 蛋白（g/kg/天） | 88kg 对应 |
|---|---|---|
| 一般成年人（久坐） | 0.8–1.2 | 70–106 g |
| 力量训练 + 减脂 | 1.6–2.0 | 141–176 g |
| 本项目现在 | 1.6 起，上限 2.0 | 140.8 g |

如果希望更保守（例如按 1.2–1.6 g/kg），改 `PROTEIN_MIN/MAX_G_PER_KG` 即可，
或者告诉我，我把它做成档案里的一个选项。
