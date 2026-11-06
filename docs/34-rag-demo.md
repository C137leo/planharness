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

## 3. 评测结果（60 条查询，id 级人工判定）

评测集 `data/rag/eval_queries.json`：每条查询由人指定"应该被检索到的 chunk id"
（按标题解析），命中 = top-k 出现任一期望 id。比"标题含关键词"严格，
且允许"多个都算对"（如"下肢力量"可以命中深蹲/腿举/弓步/腿弯举）。

三类查询：literal 30（字面） / paraphrase 15（口语转述） / food 15（营养）。

### 3.1 规模对比：600 chunks vs 850 chunks（同一套 60 条）

| 语料 | 引擎 | hit@1 | hit@3 | MRR |
|---|---|---|---|---|
| **600**（350 动作 + 250 食物） | keyword | 85% | 95% | 0.900 |
| | vector（hash） | 72% | 82% | 0.767 |
| | **hybrid** | **85%** | **95%** | **0.900** |
| **850**（600 动作 + 250 食物） | keyword | 83% | 95% | 0.886 |
| | vector（hash） | 70% | 82% | 0.750 |
| | hybrid | 82% | 95% | 0.883 |

分类别（hybrid，600 语料）：

| 类别 | hit@1 | hit@3 | 说明 |
|---|---|---|---|
| literal | 93% | **100%** | "杠铃卧推""平板支撑"这类，稳定命中 |
| paraphrase | 60% | 87% | "大腿前侧练什么""新手先练什么" |
| food | 93% | 93% | 营养条目基本稳定 |

**规模拐点结论**：动作从 350 扩到 600（总 600→850），
**hit@3 不变（95%），hit@1 掉 2-3 个点，MRR 略降**。
原因是同义/近似动作变多（杰斐逊深蹲 vs 杠铃深蹲），
词面检索的 top-1 更容易被"擦边但更匹配词面"的条目抢走。
**扩语料不会解决口语查询**：paraphrase 的 hit@1 在两种规模下都是 60%。

### 3.2 为口语做的两件事（都是显式规则，不是模型）

1. **同义词扩展** `_ALIASES`：前侧→股四头肌、后侧→腘绳肌、居家/徒手→自重、
   练胸→胸大肌/卧推、练背→划船/下拉、臀→臀大肌/臀桥…
2. **意图加权** `_intent_boost`：查询含部位/品类词时，对应 `body_part`/标签再加权。

效果（850 语料）：hybrid hit@3 90% → **95%**，paraphrase hit@3 67% → **87%**。
但 hit@1 仍只有 60% —— **规则能救一部分，救不了语义。**

### 3.3 仍失败的 3 条（600 语料下）与原因

| 查询 | 期望 | 实际 | 原因 |
|---|---|---|---|
| 减脂期做什么力量动作 | 深蹲/硬拉/卧推/划船 | 引体/侧平举/酸奶 | "减脂期"没有词面落点，规则难以判断"复合动作优先" |
| 新手先练什么动作 | 深蹲/卧推/硬拉/俯卧撑 | 面拉/引体/弯举 | 同上，"新手"是用户画像，不是文档属性 |
| 橄榄油脂肪 | 橄榄油 | 牛油果/酸奶/奶酪 | "脂肪"匹配到所有高脂食物，缺少"油"这一品类优先级 |

这三条恰好代表三类词面检索解决不了的问题：**意图/画像、否定与修饰、品类层级**。

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

## 5. 最终结论

1. **字面查询已经到顶**：600/850 两种规模下 hit@3 都是 100%，再扩语料收益接近 0；
2. **规模拐点**：动作 350→600（总 600→850）没有提升 hit@3，hit@1 反而降 2-3 点；
   当前评估下 **600 chunks 是性价比最高的点**，不必继续堆量；
3. **口语查询是天花板**：即使加了同义词与意图规则，paraphrase 的 hit@1 只有 60%；
   规则可解释、可回滚，但**不能替代语义检索**；
4. **下一步只做一件事**：接真 embedding（OpenAI 兼容接口，bge-m3 / text-embedding-3-small），
   替换 `_embed()`，用同一套 60 条评测对比；预期收益主要在这 40% 未命中的口语查询；
5. 在换 embedding 之前，**不建议**再扩大语料或加更多关键词规则——投入产出比在下降。

## 6. 复现命令

```bash
cd backend
.venv/bin/python scripts/fetch_rag_data.py      # 拉动作库（GitHub API）+ USDA Foundation（若未缓存）
.venv/bin/python scripts/build_rag_corpus.py --full --exercises 350 --foods 250
.venv/bin/python scripts/build_eval_set.py      # 由人工目标标题解析出 60 条评测 id
.venv/bin/python scripts/eval_rag.py            # 三种引擎对比
.venv/bin/python -m pytest tests/test_rag.py -q
```

## 7. 已知边界

- 43 个动作里有 1 个翻译失败，回退成英文要点；
- 动作翻译是模型产出，**未人工校对**；
- Foundation 以生食材为主，缺"中餐成品"（外卖/菜品），中国食物成分表需授权后再补；
- 外部语料与自建知识库尚未做去重/冲突处理；
- 评测集只有 16 条，只能回答"方向对不对"，不能当效果保证。
