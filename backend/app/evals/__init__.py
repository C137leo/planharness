"""任务级评测（Evals）：8 个场景、8 个指标、一份可提交的报告。

和 `tests/` 的区别：
- `tests/` 保证「代码没写坏」；
- `evals/` 回答「这条任务链在端到端上是否达标」，并产出**可对比的历史指标**。

跑法：`python -m app.evals`，报告写到 `evals/reports/latest.{json,md}`。
"""
