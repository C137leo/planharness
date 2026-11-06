# RAG 实验：free-exercise-db + USDA 混入检索

## 1. 这次做了什么

从两份许可最干净的数据源各取一部分，做成**可跑、可评测**的 RAG 语料：

| 数据源 | 取了多少 | 许可 | 产物 |
|---|---|---|---|
| [free-exercise-db](https://github.com/yuhonas/free-exercise-db) | **43 个常见动作**（深蹲/卧推/硬拉/划船/推举/核心…） | The Unlicense（公共领域） | 中文标题 + 中文动作要领（模型翻译）+ 肌群/器械/难度 |
| [USDA FoodData Central](https://fdc.nal.usda.gov/) Foundation Foods | **31 种常见食物**（鸡胸/牛里脊/三文鱼/鸡蛋/燕麦/糙米/西兰花…） | 公共领域 / CC0-1.0 | 每 100g 热量·蛋白·脂肪·碳水 |

合计 **74 chunks**，落在 `backend/data/rag/chunks.jsonl`；
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

## 3. 评测结果（16 条中文查询）

`scripts/eval_rag.py`：11 条字面查询 + 5 条口语/同义转述；命中 = top-k 标题包含期望关键词。

| 引擎 | hit@1 | hit@3 |
|---|---|---|
| keyword | **90%** | **95%** |
| vector（hash） | 75% | 90% |
| hybrid | **90%** | **95%** |

未命中明细（hybrid）：

```
✗ 减脂期主食选什么  期望 糙米/燕麦/红薯/土豆
  实际 引体向上 / 宽握高位下拉 / 希腊酸奶
```

**结论（诚实版）**：

- 74 条语料 + 字面重合高的查询，**BM25 已经够用**，向量（尤其哈希向量）没有增益；
- 唯一失败的 query 是典型的"零词面重合"（主食 → 米饭/燕麦），这正说明：
  **要解决同义/口语，需要真正的句向量模型，而不是 hash 向量**；
- hybrid 与 keyword 持平，说明当前 RRF 没有带来额外收益——**没有证据就不该上复杂度**。

## 4. 数据质量踩的坑（都记在脚本里）

1. **USDA 自动挑条目会挑错**：搜 `salmon` 选到 "Fish oil, salmon"、`brown rice` 选到 "Brown rice, puffed"。
   改成按描述重合度打分 + 品类黑名单，最后用**显式索引**锁定 31 条；
2. **Energy 单位混用**：很多 raw 食物只给 kJ，直接取 kcal 会得到 None；
   补了 kJ→kcal（÷4.184）与宏量回算；
3. **DEMO_KEY 限流**：USDA 搜索接口很快 429，所以改用 **Foundation 全量 JSON（697KB zip）** 本地筛选；
4. **翻译被思考 token 吃掉**：`deepseek-flash` 默认思考，`max_tokens=1500` 时出现
   `finish_reason=length` 且 content 为空；改成 4000 + `reasoning_effort=low` + 每批 6 条；
5. **哈希向量会碰撞**：`zzzzqqqq` 也能算出 0.24 余弦并命中"香蕉"；
   加"至少一个 token 重合"的门槛后才归零。

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
