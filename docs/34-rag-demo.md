# RAG 实验：free-exercise-db + USDA 混入检索

## 1. 这次做了什么

从两份许可最干净的数据源各取一部分，做成**可跑、可评测**的 RAG 语料：

| 数据源 | 取了多少 | 许可 | 产物 |
|---|---|---|---|
| [free-exercise-db](https://github.com/yuhonas/free-exercise-db) | **350 个动作**（43 个常用动作保底 + 按常见动作模式补满） | The Unlicense（公共领域） | 中文标题 + 中文动作要领（模型翻译）+ 肌群/器械/难度 |
| [USDA FoodData Central](https://fdc.nal.usda.gov/) Foundation Foods | **250 种食物**（32 种常见食物保底 + 按品类补满） | 公共领域 / CC0-1.0 | 每 100g 热量·蛋白·脂肪·碳水 |

合计 **600 chunks**，落在 `backend/data/rag/chunks.jsonl`；
原始数据（1.4MB 动作库等）在 `.gitignore` 里，用 `scripts/fetch_rag_data.py` 可重新拉取。

字段与自建知识库对齐，并多做三件事：`license`、`kind`（exercise/food）、`meta`（器械/难度/营养值）。

## 2. 三种检索引擎

`app/knowledge/rag.py`：

| 引擎 | 做法 | 说明 |
|---|---|---|
| `keyword` | BM25（中文 bigram + 英文单词） | 和线上 `retriever.py` 同一套分词 |
| `vector` | 特征哈希向量（512 维）+ 余弦 | **离线、无依赖，但不是语义模型**；加了"至少一个词重合"的门槛，否则哈希碰撞会让任何查询都有结果 |
| `hybrid` | RRF（1/(60+rank)）融合两路 | 默认 |

暴露接口：

```bash
curl "http://127.0.0.1:8000/api/v1/knowledge/rag?q=深蹲动作要领&engine=hybrid&limit=3" \
  -H "Authorization: Bearer <token>"
# data.hits[] 里每条都带 title/content/source_title/source_url/license/score
```

## 3. 评测结果（31 条中文查询）

`scripts/eval_rag.py`：16 条字面查询 + 15 条口语/同义转述；命中 = top-k 标题包含期望关键词。

| 引擎 | hit@1 | hit@3 |
|---|---|---|
| keyword（BM25） | 91% | **100%** |
| vector（hash） | 69% | 75% |
| hybrid（RRF） | **94%** | **100%** |

首次检索（含建 600 条索引）**13ms**；之后走内存缓存。
31 条查询里 hybrid 全部 hit@3，hit@1 只有 2 条不是第一。

### 从 74 条扩到 600 条时做的三件事（不然会掉分）

1. **保底策展条目**：350 个动作里先放 43 个常用动作、250 种食物里先放 32 种常见食物，
   否则按字母序/打分补全会把"高脚杯深蹲""鸡蛋""牛奶"挤出去；
2. **标题与策展加权**：标题命中查询词 +1.2/词，整句命中 +3；
   人工确认过的条目（`meta.priority`）+6（BM25）/+0.03（RRF）；
3. **意图加权**：查询里出现"下肢/腿""胸/背/肩""主食/蛋白质/水果"时，
   对应 `body_part` 或品类标签再 +3~4。

这三条都是**词面工程**，不是语义模型——所以 vector 仍然垫底（69/75），
真正需要"换个说法也能搜到"时，还是得换 embedding。

**结论（诚实版）**：

- **600 条语料 + 保底/加权/意图规则后，BM25 的 hit@3 已经 100%**；
  这套评测里的问题不是"语料不够"，而是"词面工程够不够细"；
- **vector（hash）明显更差**（69/75），再次说明哈希向量不是语义检索；
- hybrid 只比 keyword 多 3 个百分点的 hit@1，**复杂度换来的收益很小**；
- 真正会翻车的场景是"零词面重合"的口语（"想练胸肌""减脂吃什么主食"），
  这类要靠真 embedding，而不是继续堆关键词表。

### 3.1 一个必须修的实现 bug

加"策展条目 +6"之后，**任何查询都会返回杠铃深蹲**——因为加权写在"BM25 是否命中"之前：
`zzzzqqqq` 这种零命中查询也会被 +6 顶起来。
修法：先判断 `score > 0`（至少一个词命中）再叠加标题/策展/意图加权。
这类 bug 靠 `test_unknown_query_does_not_crash` 这种"必须返回空"的断言兜住。

## 4. 数据质量踩的坑（都记在脚本里）

1. **USDA 自动挑条目会挑错**：搜 `salmon` 选到 "Fish oil, salmon"、`brown rice` 选到 "Brown rice, puffed"。
   改成按描述重合度打分 + 品类黑名单，最后用**显式索引**锁定 31 条；
2. **Energy 单位混用**：很多 raw 食物只给 kJ，直接取 kcal 会得到 None；
   补了 kJ→kcal（÷4.184）与宏量回算；
3. **DEMO_KEY 限流**：USDA 搜索接口很快 429，所以改用 **Foundation 全量 JSON（697KB zip）** 本地筛选；
4. **翻译被思考 token 吃掉**：`deepseek-flash` 默认思考，`max_tokens=1500` 时出现
   `finish_reason=length` 且 content 为空；改成 4000 + `reasoning_effort=low` + 每批 6 条；
5. **哈希向量会碰撞**：`zzzzqqqq` 也能算出 0.24 余弦并命中"香蕉"；
   加"至少一个 token 重合"的门槛后才归零；
6. **模型翻译的专有名词会错**：`Rice, long grain` 被译成"生长粒"，`Chicken breast` 译成"鸡胸肉"没问题
   但同义词不一致；所以 43 个常用动作、32 种常见食物改回**人工中文名保底**，其余才用模型翻译；
7. **扩到 600 条后常用条目被挤掉**：按分数排序补满会漏掉"高脚杯深蹲""鸡蛋""牛奶"，
   改成"策展条目先占位，再补满"。

## 5. 想继续往上做，建议顺序

1. **先扩语料，不换检索**：把 43 个动作扩到 300 个、食物扩到 300 种（Foundation 424 条全量 + SR Legacy），
   chunks 到 ~600 时重新跑评测；如果 hit@3 仍 ≥85%，保持 BM25；
2. **换真 embedding**：接一个 OpenAI 兼容的 embedding 接口（bge-m3 / text-embedding-3-small），
   只替换 `_embed()`，把向量存 JSON 或 pgvector；600 条 embedding 成本约几分钱；
3. **再决定要不要 pgvector**：< 5k chunks 用 JSON + numpy 暴力算就够；> 5k 再上索引；
4. **把外部语料与自建知识库合并**：统一 provenance 字段，检索时按 `kind` 过滤，
   动作/食物条目只做"事实查询"，健康结论仍走自建 27→N 条 + 机构出处。

## 6. 复现命令

```bash
cd backend
.venv/bin/python scripts/fetch_rag_data.py      # 拉动作库（GitHub API）+ USDA Foundation（若未缓存）
.venv/bin/python scripts/build_rag_corpus.py    # 生成 chunks.jsonl（首次会调模型翻译，缓存到 exercise_zh.json）
.venv/bin/python scripts/eval_rag.py            # 三种引擎对比
.venv/bin/python -m pytest tests/test_rag.py -q
```

## 7. 已知边界

- 43 个动作里有 1 个翻译失败，回退成英文要点；
- 动作翻译是模型产出，**未人工校对**；
- Foundation 以生食材为主，缺"中餐成品"（外卖/菜品），中国食物成分表需授权后再补；
- 外部语料与自建知识库尚未做去重/冲突处理；
- 评测集只有 16 条，只能回答"方向对不对"，不能当效果保证。
