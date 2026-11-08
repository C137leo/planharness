# 23 所有可能出现的训练动作（做图标用）

系统在**自动生成 / 自动替换 / 用户点名新增**时可能出现的训练动作，共 **40 个**（每个动作一张图标）。

图标命名建议 `icon-<ref>.png`，`ref` 取「系统 ref」列的第一个值（连字符写法）；
表里同一个动作的两种 ref（连字符 / 下划线）共用同一张图标。

> skill 的动作目录有 300+ 个动作，但自动生成只用到下面这些。
> 只有当用户在「改计划」里点名（例如「把深蹲换成泽奇深蹲」）时，才会用到目录里的其它动作。

## 实际前端缩略图（先看这一节）

页面里的缩略图不是"每个动作一张 icon"，而是 `frontend/src/styles/thumbs.ts`：

- 6 张身体部位图（legs / hip / chest / back / core / stretch）；
- 7 张具体动作图（`pull-up` / `dead-bug` / `dumbbell-bench-press` / `deadlift` /
  `walking-lunge` / `leg-press` / `seated-row`），放在 `frontend/src/static/ex/`；
- 先按 `action_ref` 命中具体动作图，命中不了再退回 `meta.movement_mode` /
  `meta.primary_muscle` 对应的部位图。

具体动作图只配给动作一致的 ref：`seated-row` 用坐姿划船图，
`row`（哑铃划船）仍走背部图；`leg-press` 是腿举机，不配给 `hanging-leg-raise`
（举腿是另一个动作）。新增图片时先看动作模式，不要只按中文名硬配。

| # | 动作 | 系统 ref | 器械 | 主肌群 | 出现场景 | 图标文件 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 上斜杠铃卧推 | `incline-bench-press` | Barbell | Chest | 动作轮换 | `icon-incline-bench-press.png` |
| 2 | 低冲击快走 | `easy_walk` | Bodyweight | Cardio | 模板计划 | `icon-easy_walk.png` |
| 3 | 侧平板支撑 | `side-plank` | Bodyweight | Core | 动作轮换、膝痛替代 | `icon-side-plank.png` |
| 4 | 俄罗斯转体 | `russian-twist` | Bodyweight | Core | 动作轮换 | `icon-russian-twist.png` |
| 5 | 保加利亚分腿蹲 | `bulgarian-split-squat` | Dumbbell | Quads | 动作轮换 | `icon-bulgarian-split-squat.png` |
| 6 | 俯卧撑 | `push-up` / `pushup` | Bodyweight | Chest | skill 主干（自重）、模板计划 | `icon-push-up.png` |
| 7 | 全身拉伸 | `stretch` | Bodyweight | Mobility | 模板计划 | `icon-stretch.png` |
| 8 | 单臂哑铃划船 | `one-arm-dumbbell-row` | Dumbbell | Back | 动作轮换、新增动作 | `icon-one-arm-dumbbell-row.png` |
| 9 | 卷腹 | `crunch` | Bodyweight | Core | 动作轮换 | `icon-crunch.png` |
| 10 | 反向划船 | `inverted-row` | Bodyweight | Back | 动作轮换、新增动作 | `icon-inverted-row.png` |
| 11 | 史密斯卧推 | `smith-machine-bench-press` | Machine | Chest | 动作轮换 | `icon-smith-machine-bench-press.png` |
| 12 | 哑铃侧平举 | `lateral-raise` | Dumbbell | Shoulders | 动作轮换 | `icon-lateral-raise.png` |
| 13 | 哑铃俯身划船 | `dumbbell-bent-over-row` | Dumbbell | Back | 新增动作 | `icon-dumbbell-bent-over-row.png` |
| 14 | 哑铃划船 | `row` | Dumbbell | Back | 模板计划 | `icon-row.png` |
| 15 | 哑铃卧推 | `dumbbell-bench-press` | Dumbbell | Chest | skill 主干（哑铃） | `icon-dumbbell-bench-press.png` |
| 16 | 坐姿绳索划船 | `seated-row` | Cable | Back | 动作轮换 | `icon-seated-row.png` |
| 17 | 坐姿腿弯举 | `seated-leg-curl` | Machine | Hamstrings | 膝痛替代 | `icon-seated-leg-curl.png` |
| 18 | 山羊挺身 | `back-extension` | Bodyweight | Lower Back | 动作轮换、膝痛替代 | `icon-back-extension.png` |
| 19 | 平板支撑 | `plank` | Bodyweight | Core | 动作轮换、膝痛替代 | `icon-plank.png` |
| 20 | 引体向上 | `pull-up` | Bodyweight | Lats | 动作轮换 | `icon-pull-up.png` |
| 21 | 弹力带下拉 | `banded-lat-pulldown` | Resistance Band | Lats | 新增动作 | `icon-banded-lat-pulldown.png` |
| 22 | 弹力带划船 | `banded-row` | Resistance Band | Back | 新增动作 | `icon-banded-row.png` |
| 23 | 快走 | `brisk_walk` | Bodyweight | Cardio | 模板计划 | `icon-brisk_walk.png` |
| 24 | 杠铃卧推 | `bench-press` | Barbell | Chest | skill 主干（杠铃） | `icon-bench-press.png` |
| 25 | 杠铃推举 | `overhead-press` | Barbell | Shoulders | 动作轮换 | `icon-overhead-press.png` |
| 26 | 杠铃深蹲 | `squat` | Barbell | Quads | skill 主干（杠铃） | `icon-squat.png` |
| 27 | 杠铃臀推 | `hip-thrust` | Barbell | Glutes | 动作轮换、膝痛替代 | `icon-hip-thrust.png` |
| 28 | 直臂下拉 | `straight-arm-pulldown` | Cable | Lats | 动作轮换 | `icon-straight-arm-pulldown.png` |
| 29 | 罗马尼亚硬拉 | `romanian-deadlift` | Barbell | Hamstrings | skill 主干（杠铃） | `icon-romanian-deadlift.png` |
| 30 | 背包划船 | `backpack_row` | Bodyweight | Back | 模板计划 | `icon-backpack_row.png` |
| 31 | 胸支撑划船 | `chest-supported-row` | Machine | Back | 动作轮换 | `icon-chest-supported-row.png` |
| 32 | 腿举 | `leg-press` | Machine | Quads | 动作轮换 | `icon-leg-press.png` |
| 33 | 臀桥 | `glute-bridge` / `glute_bridge` | Bodyweight | Glutes | skill 主干、模板计划、膝痛替代 | `icon-glute-bridge.png` |
| 34 | 自重深蹲 | `bodyweight_squat` | Bodyweight | Quads | 模板计划 | `icon-bodyweight_squat.png` |
| 35 | 行走弓步 | `walking-lunge` | Dumbbell | Quads | 动作轮换 | `icon-walking-lunge.png` |
| 36 | 门框划船 | `doorway-row` | Doorway | Back | 新增动作 | `icon-doorway-row.png` |
| 37 | 阿诺德推举 | `arnold-press` | Dumbbell | Shoulders | 动作轮换 | `icon-arnold-press.png` |
| 38 | 高位下拉 | `lat-pulldown` | Cable | Lats | 动作轮换、新增动作 | `icon-lat-pulldown.png` |
| 39 | 高脚杯深蹲 | `goblet-squat` | Dumbbell | Quads | skill 主干（哑铃/自重） | `icon-goblet-squat.png` |
| 40 | 高脚杯深蹲（模板 ref） | `squat` | Dumbbell | Quads | 模板计划：模板里 squat 指高脚杯深蹲 | `icon-squat.png` |

## 按部位分组

### 腿部 / 膝主导 🦵（7）

保加利亚分腿蹲、杠铃深蹲、腿举、自重深蹲、行走弓步、高脚杯深蹲、高脚杯深蹲（模板 ref）

### 髋 / 臀部 🍑（5）

坐姿腿弯举、山羊挺身、杠铃臀推、罗马尼亚硬拉、臀桥

### 上肢推（胸 / 肩） 🤲（8）

上斜杠铃卧推、俯卧撑、史密斯卧推、哑铃侧平举、哑铃卧推、杠铃卧推、杠铃推举、阿诺德推举

### 上肢拉（背） 💪（13）

单臂哑铃划船、反向划船、哑铃俯身划船、哑铃划船、坐姿绳索划船、引体向上、弹力带下拉、弹力带划船、直臂下拉、背包划船、胸支撑划船、门框划船、高位下拉

### 核心 🧘（4）

侧平板支撑、俄罗斯转体、卷腹、平板支撑

### 有氧 / 放松 🏃（3）

低冲击快走、全身拉伸、快走

## 两个需要人工注意的点

1. **`squat` 同名不同动作**：模板计划里的 `squat` 是「高脚杯深蹲」，skill 里的 `squat` 是「杠铃深蹲」。
   所以表里是两行，图标也要两张——按显示名区分，不要只按 ref。
2. **同一动作两种写法**：`push-up` / `pushup`、`glute-bridge` / `glute_bridge` 等，共用一张图标即可。
