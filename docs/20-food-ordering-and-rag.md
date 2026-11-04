# 20 订餐 + 健康知识 RAG：落地方案

> 范围：只做“3 个推荐 + 平台深链”和“健康知识 RAG”。  
> 暂不做：自动打卡、14 天计划、自动下单、自动支付。

---

## 0. 先定两条边界

### 订餐

不要做“自动下单”。

原因：

- 京东/淘宝/美团没有稳定的消费者下单 MCP/API；
- 浏览器自动化会遇到登录、验证码、反爬、支付；
- 商品价格/库存随时变；
- 自动操作可能违反平台条款。

推荐：

```text
系统生成 3 个食物/食材组合
→ 每个组合带京东/淘宝/美团搜索链接
→ 用户点链接，自己选、自己下单
```

如果以后有**京东联盟 / 淘宝客 / 美团联盟** API 凭证，再实现 `AffiliateFoodProvider` 拿真实价格和推广链接。

### RAG

RAG 只放**健康知识**：

- 动作要领
- 训练部位与方法
- 安全禁忌
- 恢复与热身
- 基础营养

不要拿用户锻炼历程做向量库；锻炼记录用结构化 DB 查询/导出。  
不要让 RAG 回答诊断、用药、治疗方案。

---

## 1. 数据模型

### 1.1 `food_options`

```text
id
user_id
plan_id
plan_version_id
plan_date
meal_slot            # breakfast / lunch / dinner
title                # 鸡胸糙米碗 + 西兰花
items                # JSON: [{name, grams, kcal, protein_g}]
kcal
protein_g
reason               # 高蛋白 / 低脂 / 素食 / 预算
platform_links       # JSON: {jd, taobao, meituan}
provider             # catalog / affiliate
created_at
```

唯一约束：`(plan_id, plan_version_id, plan_date, meal_slot, title)`。

### 1.2 `knowledge_chunks`

```text
id
title
body_part            # knee / shoulder / back / core / ...
topic                # technique / safety / recovery / nutrition
content
source_title
source_url
version
embedding            # nullable，后续 pgvector
created_at
```

要求：

- 每条必须有 `source_title/source_url`；
- 内容面向“动作与训练”，不做诊断；
- 先 20～30 条，不要一上来做大规模知识库。

### 1.3 复用现有表

- `meal_plans`：已有每日餐食；
- `plan_versions`：当前计划版本；
- `FOODS`：现有本地食物库；
- `exercise_catalog`：动作元数据（部位/器械/风险）。

---

## 2. 后端：订餐

### 2.1 Provider 接口

`app/food/provider.py`：

```python
class FoodProvider(Protocol):
    def options_for_meal(
        self,
        *,
        user_id: str,
        plan_date: date,
        meal_slot: str,
        meal: dict,
        preferences: dict,
    ) -> list[FoodOption]: ...
```

默认实现：`CatalogFoodProvider`

- 从 `meal_plans` 的 items 出发；
- 在 `FOODS` 中替换蛋白/主食/蔬菜，生成 3 个变体；
- 约束：
  - 热量与目标差 ≤10%
  - 蛋白质不低于目标
  - 过敏原排除
  - 不喜欢的食物排除
- 每个变体生成平台搜索关键词。

### 2.2 平台链接

`app/food/links.py`：

```python
PLATFORM_SEARCH = {
    "jd": "https://search.jd.com/Search?keyword={q}",
    "taobao": "https://s.taobao.com/search?q={q}",
    "meituan": "https://www.meituan.com/s/{q}/",  # 上线前验证
}
```

- 链接模板放服务端配置，不要散落在前端；
- 小程序不能随意打开外部链接：复制关键词或走 web-view；
- 页面必须写“价格/库存以平台为准”。

### 2.3 API

```text
GET /api/v1/food/options?plan_id=&date=&meal_slot=
POST /api/v1/food/options/{id}/refresh-links
```

返回：

```json
{
  "options": [
    {
      "id": "...",
      "title": "鸡胸糙米碗 + 西兰花",
      "kcal": 620,
      "protein_g": 45,
      "reason": "高蛋白、低脂，避开花生",
      "items": [
        {"name": "鸡胸肉", "grams": 150, "kcal": 248},
        {"name": "糙米", "grams": 150, "kcal": 170}
      ],
      "links": {
        "jd": "https://search.jd.com/Search?keyword=...",
        "taobao": "https://s.taobao.com/search?q=...",
        "meituan": "..."
      }
    }
  ],
  "disclaimer": "价格、库存、配送范围以平台为准"
}
```

---

## 3. 后端：健康知识 RAG

### 3.1 模块

```text
app/knowledge/
├── __init__.py
├── models.py       # 可选：Pydantic 出参
├── retriever.py    # 关键词/BM25/向量
├── service.py      # search / ask
└── ingest.py       # 导入 markdown/json
```

### 3.2 检索

先用**关键词 + 元数据过滤**，不要一上来上向量：

```text
query + body_part
→ 过滤 body_part/topic
→ 中文 bigram/关键词打分
→ 取 top 3
→ 低于阈值就不回答
```

以后数据量变大，再加 embedding：

```text
keyword score + vector score → hybrid rerank
```

### 3.3 回答

`POST /knowledge/ask`：

```json
{
  "question": "深蹲膝盖疼怎么办？",
  "body_part": "knee"
}
```

返回：

```json
{
  "answer": "如果深蹲时膝盖疼，先停止高屈膝动作，改用臀桥/腿弯举等低冲击替代；如疼痛持续或肿胀，请就医。",
  "citations": [
    {
      "id": "knee-01",
      "title": "膝痛时的动作替代",
      "source_title": "ACSM 运动测试与处方指南（摘要）",
      "source_url": "..."
    }
  ]
}
```

规则：

- 没有检索到 → 明确说“暂无足够依据”，不编；
- 回答必须带 citations；
- 出现胸痛/晕厥/严重肿胀 → 停止并建议就医；
- 不做诊断、不给药物建议。

### 3.4 内容清单

先做 20～30 条：

```text
深蹲 / 硬拉 / 推 / 拉 / 核心
膝、肩、腰的替代动作
热身、冷身、恢复
RPE / 渐进超负荷
蛋白质 / 热量基础
过敏原与食物替换
红旗症状
```

### 3.5 RAG Evals

10 条问题，断言：

- `recall@3`：期望 chunk 是否在 top 3；
- `citation_valid`：引用是否真实存在；
- `no_match_refusal`：无结果时是否拒绝回答；
- `forbidden_medical_advice`：是否出现诊断/用药建议。

---

## 4. 前端

### 4.1 今天吃 / 订餐 Sheet

```text
┌────────────────────────────┐
│ 今天午餐              ✕    │
│ 目标 620 kcal / 蛋白 45g   │
│ ┌────────────────────────┐ │
│ │ 鸡胸糙米碗 + 西兰花    │ │
│ │ 620 kcal · 蛋白 45g    │ │
│ │ 高蛋白、低脂           │ │
│ │ [京东] [淘宝] [美团]   │ │
│ └────────────────────────┘ │
│ ┌────────────────────────┐ │
│ │ 牛肉藜麦碗 + 时蔬      │ │
│ │ 610 kcal · 蛋白 42g    │ │
│ │ [京东] [淘宝] [美团]   │ │
│ └────────────────────────┘ │
│ 价格/库存以平台为准        │
└────────────────────────────┘
```

交互：

- 点平台按钮 → H5 `window.open`；小程序复制关键词 + 提示“去 App 搜索”；
- 不做支付、不做登录、不做自动下单；
- 3 个选项横向/纵向卡片，适合手机点选。

### 4.2 知识 Sheet

在动作卡/计划卡加：

```text
[动作要领] [为什么这样练]
```

打开知识 Sheet：

- 标题
- 2～4 段正文
- 引用来源
- “不是医疗建议”提示

### 4.3 免责声明

全局底部/设置页：

```text
本应用提供运动与饮食建议，不构成医疗建议。
如有疼痛、眩晕、胸闷等不适，请立即停止并就医。
```

删除开发提示：

- “mock 模型”
- “临时库”
- “演示补丁”
- Console 的评测说明（改为质量中心，仅管理员可见）

---

## 5. 关键代码

### 5.1 FoodOption

```python
@dataclass(frozen=True)
class FoodOption:
    title: str
    items: list[dict]
    kcal: int
    protein_g: float
    reason: str
    links: dict[str, str]
```

### 5.2 平台链接

```python
def build_links(keyword: str) -> dict[str, str]:
    q = quote(keyword)
    return {
        "jd": f"https://search.jd.com/Search?keyword={q}",
        "taobao": f"https://s.taobao.com/search?q={q}",
        "meituan": f"https://www.meituan.com/s/{q}/",
    }
```

### 5.3 知识检索

```python
def search_knowledge(
    db: Session,
    query: str,
    *,
    body_part: str | None = None,
    limit: int = 3,
) -> list[KnowledgeChunk]:
    # 1) 元数据过滤
    # 2) 中文 bigram/关键词打分
    # 3) 低于阈值返回 []
    ...
```

### 5.4 前端打开链接

```ts
function openPlatform(platform: 'jd' | 'taobao' | 'meituan', url: string) {
  // #ifdef H5
  window.open(url, '_blank')
  // #endif
  // #ifdef MP-WEIXIN
  uni.setClipboardData({ data: url })
  uni.showToast({ title: '链接已复制，请到 App 打开', icon: 'none' })
  // #endif
}
```

---

## 6. 实施顺序

| 阶段 | 内容 | 时间 |
| --- | --- | --- |
| 1 | 三选项 Provider + 平台深链 + API | 1 天 |
| 2 | 今天吃 Sheet + 三卡片 + 免责声明 | 0.5 天 |
| 3 | knowledge_chunks + 20 条内容 + 检索 API | 1 天 |
| 4 | 知识 Sheet + 引用 + RAG Evals | 1 天 |

先做订餐，因为数据链短；RAG 后做，因为要整理内容和评测。

---

## 7. 需要确认

1. 你有没有京东联盟/淘宝客/美团联盟 API 凭证？  
   没有就按深链做。
2. 推荐的是**食材**还是**成品餐**？  
   默认：京东/淘宝推食材，美团推成品餐。
3. 知识库内容你希望偏“动作要领/安全”，还是也包含营养/恢复？  
   默认：动作 + 安全 + 恢复，营养只做基础。
4. 小程序是否接受“复制关键词，去 App 搜索”？  
   默认接受；否则需要 web-view 业务域名。
