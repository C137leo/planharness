# agent-integration —— 把 healthy-fitness-coach 接进你自己的 agent

三层结构，可只用其中一层：

```
healthy-fitness-coach/            ← skill 本体（原封不动，作为 vendor 依赖）
        ↑ require
agent-integration/
  ├── coach.js                    ← 门面层：4 个业务函数 + 1 个安全门（确定性）
  ├── tools.js                    ← 6 个 function-calling schema + dispatch
  ├── agent-loop.js               ← 最小 ReAct 循环（接任意 OpenAI 兼容 endpoint）
  └── run-demo.js                 ← 不开模型，直接看每个函数的返回
```

## 快速验证（零安装、零联网）

```bash
cd agent-integration
node run-demo.js          # 看 4 个函数的完整返回
node agent-loop.js --mock # 看工具调用链路 + 红旗拦截
```

`healthy-fitness-coach/references/*.js` 是零依赖 CommonJS，不需要 `npm install`。
只有训记 MCP 连接器（`healthy-fitness-coach-plugin/mcp/xunji`）才需要 `npm ci`。

## 接真实模型

```bash
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_API_KEY=sk-...
export LLM_MODEL=deepseek-chat
node agent-loop.js "我30岁175cm80kg，想减脂，每周3天每次40分钟，只有哑铃和垫子"
```

## 6 个 tool

| tool | 作用 | 对应 skill 源码 |
|---|---|---|
| `coach_safety_gate` | 红旗筛查，`blocked=true` 时禁止一切处方 | `references/safety-context.js` |
| `coach_plan_program` | 计划骨架：天数/时长/动作模式/组次/RIR/最低版本 | `references/program-rules.js` |
| `coach_today_session` | 今日适配：proceed/regress/stop，只改 1 个变量 | `references/readiness-engine.js` |
| `coach_nutrition_targets` | BMR/TDEE/热量/蛋白区间（**本项目新增层**） | `references/nutrition.md` 的规则 |
| `coach_review_week` | 事实→判断→决策日志 | `references/review-decision-engine.js` |
| `coach_read_reference` | 按白名单读 `references/*.md` | 23 个 md 文件 |

## 三条必须遵守的接入纪律

1. **原话必须转发**。红旗可能只出现在用户原话里。`compileProgramRules` 只检查画像中写明的风险，
   不读 `instruction` —— 直接把 profile 丢给它就会绕过安全门。`coach.js` 已修正这一点，
   但你自己新增工具时要保持同样做法。
2. **不要并行调用多个业务工具**。顺序是 安全门 → 计划 → 今日 → 营养 → 复盘。
   安全门没过就不要再调后面的。
3. **`needs_input` 只问缺的字段**。`missing_fields` 是引擎算出来的，不要自己加问题，
   也不要给没提供的字段填默认值。

## 落盘

`coach_plan_program(profile, { outputDirectory: '../fitness-reports' })` 会生成
`CURRENT_PROGRAM.md`，且**不覆盖已有文件**（沿用 `output-routing.js` 的 `-2/-3` 后缀语义）。
`fitness-reports/` 已在 `.gitignore` 中。
