# 21 把教练 skill 接进产品：生成 → 确认 → 执行

> 这一轮解决四件事：今天页没有计划、画像不能录、计划不是模型 + skill 生成的、补丁写死了膝痛。

## 1. 责任划分（先定这个，后面都是细节）

| 角色 | 负责什么 | 不负责什么 |
| --- | --- | --- |
| **healthy-fitness-coach skill** | 训练结构、动作选择与降级、RIR、安全筛查、BMR/TDEE/宏量 | 生成文案、决定要不要提交 |
| **模型（DeepSeek）** | 读 skill 的入口工作流，决定调哪个工具、解释为什么这么排 | 编造数字、绕过安全门 |
| **本地安全门（Python）** | 过敏原、训练日上限、目标热量区间、红旗结论 | 算营养、选动作 |
| **用户** | 确认 / 要求修改 | —— |

一句话：**skill 算，模型编排，代码否决，用户确认。**

## 2. skill 怎么接进来的

skill 是 Node 写的确定性引擎（`agent-integration/coach.js` + `web/lib/nutrition.js`），
我们的后端是 Python。做法是**只做协议转换，不重写逻辑**：

```
backend/app/skill/runner.js     ← 收 JSON、调 skill、吐 JSON
backend/app/skill/bridge.py     ← SkillBridge：subprocess + 缓存 + 超时 + 显式降级
```

可用命令：`meta` / `skill_md` / `tools` / `safety_gate` / `screen_profile` /
`plan_program` / `today_session` / `nutrition_targets` / `nutrition_plan` /
`review_week` / `read_reference`。

三条接入纪律照抄 skill 自己的要求：

1. **原话必须转发**：红旗症状可能只出现在用户原话里，画像里没有。
2. **工具串行**：安全门 → 计划 → 营养，安全门没过不调后面。
3. **缺字段就问**：`needs_input` 返回什么字段就问什么，不替用户编默认值。

动作目录也从 skill 生成，避免两套名字对不上：

```bash
SKILL_DIR=/path/to/skill python scripts/sync_exercise_catalog.py
# → app/domain/exercise_catalog.py：302 个动作，其中 56 个标注为膝痛风险
```

## 3. 生成流程

```
用户说一句目标
   → POST /plans/drafts
   → PlanningAgent：模型带 skill 的 tools 走 function-calling
        （无 Key/超时/解析失败 → 同一套工具的确定性编排）
   → 组装 payload：program + nutrition + days + meals + warnings
   → 本地安全门（过敏原 / 训练日 / 热量区间 / 红旗）
   → plan_drafts 落库（status = ready / clarifying / invalid / blocked）
   → 用户「再改改」（≤3 轮，旧草案 superseded）或「确认」
   → POST /plans/drafts/{id}/confirm → 新 PlanVersion + MealPlan + 审计 plan_confirmed
```

草案阶段**不写任何版本**；确认后才有新版本；拒绝或取消不影响现有计划。

## 4. 数据和接口

新增表：`plan_drafts`（草案）、`meal_plans`（每餐食材与热量）。
扩展现有表：`profiles.goal / goal_note / diet_preferences`、`goals.raw_text`、
`plans.source / nutrition / program`、`plan_actions.kcal / meta`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| PUT | `/me/profile` | 录目标 / 体型 / 器械 / 过敏原 / 饮食偏好 |
| POST | `/plans/drafts` | 生成草案（模型调 skill） |
| GET | `/plans/drafts/{id}` | 取草案 |
| POST | `/plans/drafts/{id}/feedback` | 再改改（≤3 轮） |
| POST | `/plans/drafts/{id}/confirm` | 确认 → 落版本 |
| POST | `/plans/drafts/{id}/reject` | 不要了 |
| GET | `/patch-presets` | 预设改动入口（名称 + 示例 + 提示） |

`/plans/current` 现在会带回 `source / nutrition / program / meals / today`，
其中 `today` 是「今天练什么 + 今天吃什么」，前端不用自己算星期。

## 5. 补丁不再是膝痛

原来是「生成膝痛补丁」一个按钮、一个写死的场景。现在：

- `GET /patch-presets` 给的是**用户能这么说的示例**：今天想休息 / 受伤了先停练 /
  受伤了换部位 / 感觉进步了加强度 / 调饮食 / 这周没时间；
- 用户也可以完全自由输入，`app/agent/change_agent.py` 让模型输出
  「改哪个 slot_key、换成什么动作」的 JSON，再由确定性校验决定能不能用：
  动作是否存在、槽位是否存在、是否同部位、是否超天数、是否重复改同一槽；
- 任何一条校验失败就整份拒绝并追问一句话，不做"部分应用"；
- 没有模型时退回规则表（`planner.parse_intent` + `plan_operations`），接口行为一致。

安全门也补了一个真问题：模板计划用 `squat`，skill 计划用 `goblet-squat`，
按字符串比对会漏检，现在统一走 `is_knee_risky()`（归一化 ref + skill 目录标注 + `movement_mode`）。

## 6. 前端

TabBar：今天 / 计划 / 进展 / 我的。今天页现在有三块：

1. **今天练**：动作、组次/时长、主肌群；
2. **今天吃**：每餐食材克数与热量、当天合计 vs 推荐摄入、蛋白目标；
3. **改计划**：六个预设卡片 + 「自己说一句」+「换/生成整份计划」。

计划页的旧文案「去逐条确认 / 生成膝痛补丁」已改成「去今天页修改计划」；
按钮统一换成小尺寸胶囊（`.btn-sm`），只在主操作上保留实心色。

视觉改成暖色食谱类 App 的观感（参考 fitailabs 那类产品）：

| 变量 | 值 | 用在哪 |
| --- | --- | --- |
| `--canvas` | `#FDF2E9` | 页面底色（暖奶油） |
| `--surface` | `#FFFFFF` | 卡片，圆角 20px + 很淡的暖色阴影 |
| `--primary` | `#E2552B` | 主操作、选中胶囊、链接、热量柱子 |
| `--primary-soft` | `#FCEAE1` | 次级块、摄入目标条 |
| `--ink` / `--muted` | `#2B1A12` / `#8A7466` | 正文是暖黑，次要信息是暖灰 |

版式上：顶部一张渐变 hero 卡（今天最重要的一句话 + 时长徽标），下面是白卡片列表，
动作/餐食都带圆形缩略图占位（没有实拍图时用 emoji 而不是空白），改动入口是横向滚动的胶囊行。
tabBar 仍是文字（uni-app 的图标位需要准备 PNG 资源，这轮没做）。

顺手修了三个真实的布局 bug（都是这次截图暴露的）：

| 问题 | 根因 | 修法 |
| --- | --- | --- |
| 输入框溢出卡片右侧 | 只给 `view/text` 设了 `box-sizing: border-box`，`uni-input` / `uni-textarea` 还是 `content-box`，`width:100% + padding` 就多出 24px | 全局把表单控件也纳入 border-box；`.input` 再加 `max-width:100%` |
| 抽屉底部按钮被 tabBar 压住 | 抽屉 `z-index: 61` 低于 uni-app H5 tabBar 的 998，且底部没有为 tabBar 留空间 | 抽屉与遮罩提到 1200/1201，底部 padding 加上 `var(--window-bottom)` |
| 桌面端整页拉成一条 | 手机优先的布局没有最大宽度 | `.page { max-width: 760px; margin: 0 auto }`，Console 用 `.page.wide` 保持全宽 |
| 输入框变成空白条、点不进去 | `box-sizing: border-box` 之后，uni-input 默认的 `height: 1.4em` 被 padding 吃光，内容区高度为 0 | 单行统一 `height: 46px; padding: 0 14px`，多行 `height: 96px; padding: 12px 14px`，并让内部 wrapper/input 撑满 100% |
| 预设胶囊在卡片右侧被截断 | `overflow-x: auto` 的单行滚动在卡片内边被裁掉半个胶囊 | 改成 `flex-wrap: wrap`，永不横向截断 |

## 6.1 两个真实 bug（截图暴露的）

**过敏原识别是精确字符串**：用户填「海鲜过敏」，代码只认「海鲜」，于是食谱照样推荐虾。
这是安全问题——不能要求用户写法恰好标准。改成 `app/domain/allergens.py` 的关键词匹配：

| 用户写法 | 识别结果 |
| --- | --- |
| 海鲜过敏 / 对海鲜过敏 / 甲壳类 / 海产品 / shellfish | `seafood` |
| 乳制品 / 牛奶 / 芝士 / dairy | `dairy` |
| 花生 / peanut | `peanut` |
| 豆制品 / 豆腐 / soy | `soy` |
| 牛肉 / 牛羊肉 / beef | `beef` |

认不出来的词**不假装安全**，而是写进 cautions：「这些忌口我没能自动识别，请确认」。
生成食谱和本地安全门用同一个解析器，避免「生成时排除了、校验时又放行」这种口径不一致。

**弹窗是贴底抽屉**：改成居中弹窗（`top: 50%` + `translate(-50%,-50%)`，宽度 `100% - 32px`，
最大 560px，上下都留余量），不再被 tabBar 压住；「再改改」这一行改成输入框吃剩余宽度、
按钮 `white-space: nowrap` 不换行。

## 6.2 反馈必须能改计划（否则「再改改」是假的）

用户点「再改改」后只看到解释文案变化、计划没变。根因：训练结构由 skill 按画像确定算出，
**反馈根本进不到结构里**，模型只是重新写了一遍说明。

现在反馈会落到确定性变换上（`app/domain/revisions.py`）：

| 反馈关键词 | 变换 |
| --- | --- |
| 动作重复 / 要有区别 / 换动作 | 每个动作换成同肌群列表里的下一个（跨天不重复），第一天也会变 |
| 膝 / 膝疼 / 蹲太多 | 膝主导动作换成髋导向或核心动作 |
| 加强度 / 加量 | 每个力量动作 +1 组（上限 4） |
| 太累 / 轻松点 | 每个动作 -1 组（下限 1），并去掉每场最后一个动作 |
| 蛋白 / 丰富 / 轮换 / 鸡胸肉 | 餐食按天顺延一天，内容真的换 |

每次变换写进 `revisions`，草案弹窗里直接展示「这一轮改了什么」，用户不用猜。

**但模糊规则还不够**：用户真正常说的是具体指令，比如
「把红薯换成土豆，并新增一个练背动作」。第一版只认模糊关键词，结果模型在摘要里
写"已经换了"，计划纹丝不动——这是最糟糕的一种"看起来做了"。现在新增
`app/domain/draft_edits.py`，把具体编辑拆成可验证操作：

| 句式 | 操作 |
| --- | --- |
| 「把红薯换成土豆」 | 替换食材，克数按同等热量换算，重算宏量 |
| 「新增一个练背动作」 | 每个训练日追加一个该部位的动作（已有则不重复加） |
| 「去掉高脚杯深蹲」 | 按动作名或 slug 删除 |
| 「把深蹲换成臀桥」 | 动作替换，保留组次 |

执行顺序：**具体编辑 → 模糊变换 → 都没有就诚实标注**。
最后一种情况会把摘要改写成「（本轮没有改动计划内容）」，并给出可用的句式示例，
不让模型替你吹。首次生成的第一句话同样会过一遍具体编辑。

顺带修了两个小问题：蛋白源同一天不再三顿都是鸡胸肉（按天维护已用集合，并补了瘦牛肉）；
过敏字段里填「无」不再被当成"没识别出的忌口"提示。

## 7. 已验证 / 未验证

已验证（可复现）：

**真实模型 + 真实 skill 跑通**（本机 `.env` 配了 DeepSeek Key）：

```
一次生成 = coach_safety_gate → coach_plan_program → coach_nutrition_targets → 模型收尾
usage: 10827 prompt / 919 completion tokens → 约 0.029 元
产出: 3 个训练日 / 28 条餐食 / 2230 kcal / 蛋白 109–156 g
```

模型的收尾还主动指出了两个问题（Day 2 与 Day 1 动作重复、工具没给休息秒数），
说明它读的是工具返回，而不是自己编。

补丁的六条预设也逐条过了真实模型：

| 用户原话 | 结果 | 改动 |
| --- | --- | --- |
| 今天想休息 | ready | 删掉当天训练槽位 |
| 膝盖受伤了，先停止所有腿部训练 | ready | 删训练 + **代码补全**仍涉及膝的槽位 |
| 这周只能练两天，每次 30 分钟 | ready | 删多余训练日 |
| 这周感觉轻松，想加一点强度 | ready | 换更难的动作变式 |
| 晚餐想吃清淡一点，少油 | ready | 晚餐换成低热量餐食 |
| 腰不舒服，想练练上肢 | ready | 避开腰主导动作 |

其中「只改了一部分」是真实存在的问题：模型第一次只删了 1 个膝主导动作。
现在 `cover_injured_slots()` 会在应用前补齐，模型的提议可以不全，但结果必须满足硬约束。



- `pytest`：**116 passed / 1 skipped**，其中 `tests/test_coach.py` 8 条覆盖
  真实 skill 调用、半自动生成、3 轮上限、确认落库、拒绝、降级安全门；
  `tests/test_patch_requests.py` 11 条覆盖自由文本改动、休息/减天数、受伤补全
- `pytest` 里 `SkillBridge` 若找不到 skill 会 skip 而不是假通过
- `python -m app.evals`：**11/11 场景、10/10 指标达标**（E09 生成 / E10 自由文本改动 /
  E11 轮次上限为新增，全程约 2.9 秒；旧 8 条不受影响）
- `python -m app.evals --mutants`：6/6 变异体仍被抓住
- `npm run build:h5` 与 `tsc --noEmit` 通过

未验证：

- **浏览器里的真实观感**：当前环境没有浏览器，页面只做了构建期检查。
- 模型路径的手工验证只覆盖了少数几条原话，失败率与稳定性没有统计（单元测试跑的是
  确定性编排，以免测试消耗额度）。
- 小程序端未回归（需要 AppID）。
