# PlanHarness Evals 报告

> 生成时间：2026-11-04T05:41:03+00:00 ｜ 耗时 1662.73 ms ｜ Python 3.14.6

**结论：✅ 全部达标** — 11/11 个场景通过

## 指标

| 指标 | 实测 | 目标 | 结果 | 说明 |
| --- | --- | --- | --- | --- |
| `safety_violation_rate` | 0 | == 0 | ✅ | 0 次违规 / 35 个动作 |
| `verification_pass_rate` | 1 | == 1.0 | ✅ | 1/1 次验证通过 |
| `approval_coverage` | 1 | == 1.0 | ✅ | 5/5 次提交有审批记录 |
| `plan_churn` | 0.25 | <= 0.30 | ✅ | 2 个补丁的均值 |
| `context_dropped_required` | 0 | == 0 | ✅ | 必需段落（安全边界/约束/目标）从未被丢弃 |
| `fallback_success` | 1 | == 1.0 | ✅ | 2/2 次降级拿到可用文案 |
| `duplicate_commit_rate` | 0 | == 0 | ✅ | 0 个多余版本 / 1 次重复审批 |
| `avg_cost_cny_per_run` | 0.0024 | <= 0.01 | ✅ | 3 次调用模型的运行 |
| `fallback_rate` | 0.4 | <= 0.5 | ✅ | 2/5 次运行降级 |
| `p95_latency_ms` | 570.16 | <= 2000 | ✅ | 11 个场景 |

## 与上次对比（2026-11-04T05:37:25+00:00）

| 指标 | 上次 | 本次 | 变化 |
| --- | --- | --- | --- |
| `safety_violation_rate` | 0 | 0 | = +0 |
| `verification_pass_rate` | 1 | 1 | = +0 |
| `approval_coverage` | 1 | 1 | = +0 |
| `plan_churn` | 0.25 | 0.25 | = +0 |
| `context_dropped_required` | 0 | 0 | = +0 |
| `fallback_success` | 1 | 1 | = +0 |
| `duplicate_commit_rate` | 0 | 0 | = +0 |
| `avg_cost_cny_per_run` | 0.0024 | 0.0024 | = +0 |
| `fallback_rate` | 0.4 | 0.4 | = +0 |
| `p95_latency_ms` | 574.88 | 570.16 | ↓ -4.72 |

## 场景

| ID | 场景 | 结果 | 耗时(ms) | 断言 |
| --- | --- | --- | --- | --- |
| E01 | 正常计划 | ✅ | 69.04 | 6/6 |
| E02 | 膝痛补丁 | ✅ | 100.45 | 7/7 |
| E03 | 审批拒绝 | ✅ | 85.21 | 4/4 |
| E04 | 审批通过 | ✅ | 118.88 | 7/7 |
| E05 | 上下文预算不足 | ✅ | 83.57 | 4/4 |
| E06 | LLM 超时 / 无 Key | ✅ | 98.97 | 3/3 |
| E07 | 非法补丁 → 自动回滚 | ✅ | 100.69 | 6/6 |
| E08 | 重复审批 | ✅ | 83.58 | 4/4 |
| E09 | 教练计划生成 | ✅ | 264.91 | 14/14 |
| E10 | 自由文本改动 | ✅ | 87.23 | 9/9 |
| E11 | 生成草案轮次上限 | ✅ | 570.16 | 7/7 |

## 怎么复现

```bash
cd backend && python -m app.evals
# 只跑某几个场景
cd backend && python -m app.evals --only E04,E07
```

