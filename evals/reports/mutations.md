# PlanHarness 变异测试报告

> 生成时间：2026-11-03T18:53:13+00:00 ｜ 变异得分 **100%**（6/6）

| ID | 变异体 | 弄坏了什么 | 预期失败场景 | 实测失败 | 结果 |
| --- | --- | --- | --- | --- | --- |
| M1 | 退化成全量重生成 | 把 _diff 改成「几乎全改」，等价于 A 模式（裸奔）的 churn | E02 | E02 | ✅ 被杀 |
| M2 | 驳回落成通过 | reject_patch 实际调用 approve_patch，用户不同意也提交 | E03 | E03 | ✅ 被杀 |
| M3 | 验证器恒通过 | verify_commit 永远返回 passed=True，提交后不再核对事实 | E07 | E07 | ✅ 被杀 |
| M4 | 预算配置被忽略 | 上下文装配永远用超大预算，required 超限也不会告警 | E05 | E05 | ✅ 被杀 |
| M5 | 降级文案为空 | fallback_explanation 返回空串，模型不可用时用户看不到任何解释 | E05, E06 | E05, E06 | ✅ 被杀 |
| M6 | 安全门被关闭 | _safety_check 恒返回空，禁忌动作与超天数全部放行 | E02 | E02 | ✅ 被杀 |

## 怎么复现

```bash
cd backend && python -m app.evals --mutants
```

