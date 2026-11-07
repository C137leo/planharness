# 关键词检索到底怎么做的？这算不算 RAG？

## 0. 直接回答

- **现在这个规模（600 条外部语料 + 30 条自建知识），关键词检索够用**，
  而且它比向量更可解释、更便宜、结果更稳定；
- 但"关键词检索"在我们项目里其实是**两套实现**，服务两种不同语料；
- **算 RAG**——RAG 是"先检索、再让模型基于检索结果生成并给出处"的架构，
  检索器用 BM25 还是向量都可以。我们用的是 **BM25/词面检索 + 引用校验** 的 RAG。

## 1. 两套检索，别混在一起

| | 自建知识库（30 条） | 外部 RAG 语料（600 条） |
|---|---|---|
| 代码 | `app/knowledge/retriever.py` | `app/knowledge/rag.py` |
| 语料 | 动作要领/安全/恢复/营养（带机构出处） | free-exercise-db 350 动作 + USDA 250 食物 |
| 算法 | **BM25**（bigram + 单字，+ 标题重复/body_part 加分） | **BM25**（+ 标题/策展/意图加权）+ 哈希向量（对照） |
| 谁在用 | 今日 Agent 的 `search_knowledge` 工具、`/knowledge/ask` | `/knowledge/rag` 对比接口、评测脚本 |
| 目的 | 回答"该怎么做/要不要就医"，必须给机构出处 | 回答"这个动作/食物是什么、多少营养" |

两套的检索结果目前**没有合并**：健康结论走自建库，事实数据走外部语料。
合并是后面的事（要统一 provenance 字段与冲突处理）。

## 2. 自建库的打分（retriever.py）

### 2.1 分词：中文 bigram + 单字 + 英文单词

```python
# app/knowledge/retriever.py: tokenize
tokenize("深蹲时膝盖疼怎么办")
# ['深蹲','蹲时','时膝','膝盖','盖疼','疼办','深','蹲','时','膝','盖','疼','办']
```

- 先把标点换空格、转小写；
- 英文/数字按词切；中文按**相邻两字**切（bigram），**再把单字也加进去**
  （"膝""睡"这种核心单字比 bigram 更稳）；
- 去掉一小张停用词表（的/了/吗/怎么/为什么…）。

### 2.2 打分：BM25 + 标题重复 + 元数据

第一版是手写加权（标题 3 / 标签 2 / 正文 1），84 条评测后输给 BM25，
现在 `search()` 用的是：

```python
# app/knowledge/retriever.py
BM25_K1 = 1.5
BM25_B = 0.75
BM25_MIN_SCORE = 6.0

# 文档文本 = 标题（重复一次）+ 正文 + tags
# 标准 BM25 + body_part 加分（同部位 +2，general +0.5）
```

- **标题重复一次**：43 条语料里标题是最强意图信号，BM25 本身不区分字段；
- **body_part 加分**：调用方知道用户问的是膝/肩/背时，同部位条目优先；
- **阈值 6.0**：在 84 条评测上不损失 hit@1/hit@3，同时挡掉"今天天气"这类完全无关问题；
- 取 top-k（k=3）。

实测（`深蹲时膝盖疼怎么办`，body_part=knee）：

```
30.7  knee-pain-swap   膝痛时的动作替代
27.3  squat-cue        深蹲的动作要领
 9.9  kb-ex-legs       练腿的常见动作清单
```

为什么这条链能答对：查询里的"深蹲/膝盖/疼"分别命中了标题、tags 和正文，
IDF 会压低"动作"这类高频词，标题重复又强化了"膝盖"这个意图。

改前/改后（同一套 84 条）：

| 方法 | hit@1 | hit@3 | MRR |
|---|---|---|---|
| 旧手写加权 | 82% | 96% | 0.903 |
| BM25 + 标题 + body_part | **92%** | **100%** | **0.971** |

详见 `docs/37-knowledge-search-eval.md`。

### 2.3 没有向量，怎么保证"相关"？

- 语料只有 43 条，且标题就是用户会说的词（深蹲/卧推/膝痛…），**词面重合度高**；
- BM25 的 IDF/长度归一化把"训练"这类高频词压下去，标题重复补上字段信号；
- 元数据（`body_part`、`topic`）把搜索空间再收窄一层；
- 引用必须真实存在（`validate()` 会查库核对 id），检索错了会被兜底暴露，
  而不是悄悄编一段话。

## 3. 外部语料的检索（rag.py）

### 3.1 BM25（主引擎）

```python
# 经典 BM25：k1=1.5, b=0.75
score(q, d) = Σ_t idf(t) · tf(t,d)·(k1+1) / (tf(t,d) + k1·(1-b+b·|d|/avgdl))
idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
```

三个直觉：

- **tf 饱和**：一个词出现 10 次不会比出现 2 次强 5 倍（k1 控制饱和）；
- **长度归一化**：长文档不占便宜（b 控制惩罚强度）；
- **idf 加权**：越少见的词越值钱。实测 `深蹲` df=29 → idf 3.01，`膝盖` df=11 → idf 3.96。

在此之上加了四个**显式**后处理（都能在代码里看到、能关掉）：

| 加成 | 目的 |
|---|---|
| 标题命中 +1.2/词、整句命中 +3 | 标题直接出现查询词应明显更相关 |
| 策展条目 +6 | 人工确认过的 43 动作/32 食物优先，压住冷门同义变体 |
| 意图加权 +3~4 | 查询含"下肢/胸/背/肩"或"主食/蛋白质/水果"时对相应 `body_part`/品类加权 |
| 同义词扩展 | 前侧→股四头肌、后侧→腘绳肌、居家/徒手→自重、练胸→胸大肌/卧推… |

关键顺序：**先判断至少一个词命中（score>0），再叠加这些加成**。
我们踩过坑：加成写在命中判断之前，`zzzzqqqq` 也会被顶出"杠铃深蹲"。

### 3.2 向量与混合（对照，不是主引擎）

- `vector`：把 token 哈希到 512 维（特征哈希）+ 余弦；加"至少一个 token 重合"门槛，
  否则哈希碰撞会让任何查询都有结果。**它不是语义模型**，评测里明显更差；
- `hybrid`：RRF 融合两路排名（`1/(60+rank)`），策展条目额外 +0.03。

## 4. 这算不算 RAG？

**算。** RAG 的定义是架构，不是某个算法：

```
Retrieve → Augment prompt → Generate → Ground / Cite
```

我们今日 Agent 的真实链路（`app/agent/today_agent.py`）：

1. **Retrieve**：模型调用 `search_knowledge` 工具 → `kb_search()` → `retriever.search()`；
2. **Augment**：工具结果（`data` + `citations`）作为 `role=tool` 消息塞回模型上下文；
3. **Generate**：模型写 `tips`，每条带 `source_ids`；
4. **Ground**：`validate()` 查库核对每个 `source_id` 真实存在，伪造引用直接判失败降级；
5. 用户看到的是"结论 + 可点的机构来源"。

所以：

- **`/knowledge/rag` 接口本身只是检索**（返回 hits，没有生成），严格说是 RAG 的 R 部分；
- **今日 Agent 的 `search_knowledge → tips + citations` 才是完整的 RAG**；
- 检索器是 BM25 还是向量，不影响它是不是 RAG。

## 5. 什么时候关键词够、什么时候不够

| 场景 | 关键词（BM25） | 说明 |
|---|---|---|
| 术语明确、用户会照着说（深蹲/卧推/鸡胸肉） | ✅ 够 | 实测 literal 类 hit@3 100% |
| 语料 < ~1000 条、结构清晰 | ✅ 够 | 词面重合高，规则可解释可回滚 |
| 必须给出处、可审计 | ✅ 更合适 | 检索命中即证据，没有 embedding 漂移 |
| 口语/同义/否定（"前侧""居家无器械""不是腿是臀"） | ❌ 吃力 | 实测 paraphrase hit@1 只有 60% |
| 用户画像/意图（"新手""减脂期"） | ❌ 词面无法表达 | 需要 query 理解或语义检索 |
| 语料 > 5k、跨语言 | ❌ | 关键词表维护不动 |

**最终取舍**：当前"术语型 + 事实型"问答，关键词 RAG 是最优解；
如果产品要接"自然口语健康问答"，再换 embedding，**只需要替换 `_embed()`**。

## 6. 怎么自己验证

```bash
cd backend
.venv/bin/python scripts/eval_rag.py          # 60 条 id 级评测，三引擎对比
.venv/bin/python - <<'PY'
from app.knowledge.retriever import tokenize
from app.knowledge import rag
print(tokenize("深蹲时膝盖疼怎么办"))
rag.load_chunks(); print(rag.stats())
PY
.venv/bin/python -m pytest tests/test_rag.py tests/test_knowledge.py -q
```

代码位置：`app/knowledge/retriever.py`（43 条 KB 的 BM25）、`app/knowledge/rag.py`（BM25/向量/RRF）、
`app/agent/today_tools.py::search_knowledge`（工具封装）、`app/agent/today_agent.py::validate`（引用校验）。
