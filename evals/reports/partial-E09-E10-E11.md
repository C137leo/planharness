# PlanHarness Evals 报告

> 生成时间：2026-11-03T19:06:33+00:00 ｜ 耗时 1168.82 ms ｜ Python 3.14.6

**结论：❌ 未达标** — 3/3 个场景通过

## 指标

| 指标 | 实测 | 目标 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| `safety_violation_rate` | 0 | == 0 | ✅ | 0 次违规 / 21 个动作 |
| `verification_pass_rate` | — | == 1.0 | ✅ | 本次未采样 |
| `approval_coverage` | 1 | == 1.0 | ✅ | 2/2 次提交有审批记录 |
| `plan_churn` | — | <= 0.30 | ❌ | 0 个补丁的均值 |
| `context_dropped_required` | 0 | == 0 | ✅ | 必需段落（安全边界/约束/目标）从未被丢弃 |
| `fallback_success` | — | == 1.0 | ✅ | 本次未采样 |
| `duplicate_commit_rate` | — | == 0 | ✅ | 本次未采样 |
| `avg_cost_cny_per_run` | 0 | <= 0.01 | ✅ | 0 次调用模型的运行 |
| `fallback_rate` | — | <= 0.5 | ✅ | 本次未采样 |
| `p95_latency_ms` | 639.64 | <= 2000 | ✅ | 3 个场景 |

## 场景

| ID | 场景 | 结果 | 耗时(ms) | 断言 |
| --- | --- | --- | --- | --- |
| E09 | 教练计划生成 | ✅ | 404.53 | 14/14 |
| E10 | 自由文本改动 | ✅ | 124.62 | 9/9 |
| E11 | 生成草案轮次上限 | ✅ | 639.64 | 7/7 |

## 怎么复现

```bash
cd backend && python -m app.evals
# 只跑某几个场景
cd backend && python -m app.evals --only E04,E07
```

