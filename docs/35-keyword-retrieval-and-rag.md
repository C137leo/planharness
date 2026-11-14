# 知识检索：当前实现与算法边界

## 两类知识入口

| 用途 | 源码 | 当前 API |
|---|---|---|
| 自建知识搜索/有出处问答 | knowledge/retriever.py、service.py | GET /api/v1/knowledge/search、POST /api/v1/knowledge/ask |
| 外部语料检索对比 | knowledge/rag.py | GET /api/v1/knowledge/rag |

这些接口仍在 main.py 挂载。today_agent 等独立路径也能调用知识服务；不代表生产聊天的能力菜单已经包含全部知识工具。聊天真实边界见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md)。

## 自建库检索

retriever.tokenize 将中文按 bigram 加单字、英文数字按词切分，去标点和停用词。search 使用 BM25（K1=1.5，B=0.75，默认阈值6.0），文本包含重复标题、正文和标签，并按部位/主题等元数据处理。score 保留第一版加权词面基线，供 eval_knowledge.py 比较，不是 search 的实际排序器。

service.ask 使用可检索知识及来源组织回答；无可信命中时不应伪造出处。具体来源、数量与有效语料应从当前数据库/种子和实际评测确认，不能使用旧文档中的固定条数或旧百分比代替本次测量。

## 外部语料对比

rag.py 提供 keyword、vector、hybrid。keyword 使用 BM25 与扩词/意图加权；vector 是本地 hash-ngram 向量，**不是外部语义 embedding 模型**；hybrid 用 RRF 融合排序。load_chunks 加载本地语料。原始下载与抓取脚本、数据来源文档继续保留，不能因原始文件被忽略就删除加载代码。

检索只是 RAG 的检索部分；是否真正执行“基于检索生成且验证引用”应逐调用链判断，不能因接口名含 rag 就声称完整问答已接入。历史评测结果留在原证据中，不是当前版本准确率。

保留旧基线用于对比是独立用途，不属于可安全删除的重复代码。未来是否采用语义 embedding 应以新的检索评测决定；本次未升级算法、语料或依赖。
