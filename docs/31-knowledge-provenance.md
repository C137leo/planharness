# 知识库的出处与正确性：这些内容从哪来、凭什么信

## 1. 是不是从 skill 里拷的？——不是

skill（`healthy-fitness-coach-skill`）里确实有一堆参考材料：
`references/nutrition.md`、`recovery-pain.md`、`assessment.md`、`evidence-rules.md` …
但它们是**英文/JS 的规则与工作流**，不是可以逐条引用的中文知识条目。

验证方式（可复现）：把种子里的特征句拿去 skill 目录里 grep，命中 0 次：

```bash
cd ~/PycharmProjects/healthy-fitness-coach-skill
grep -rl "膝盖朝脚尖方向" healthy-fitness-coach/references   # 无输出
grep -rl "杠铃贴近小腿"   healthy-fitness-coach/references   # 无输出
grep -rl "还能做几次"     healthy-fitness-coach/references   # 无输出
```

结论：**这 30 条是按常见运动健康问题手写的中文摘要，每条自己挂机构出处**。
skill 负责"生成计划/安全门/营养目标"的**计算**，知识库负责"解释和引用"，
两者是分开的两条数据流。

## 2. 出处怎么管（现在有的机制）

| 机制 | 位置 | 说明 |
|---|---|---|
| 必填字段 | `app/knowledge/seeds.py` | id / title / content / topic / source_title / source_url |
| 域名白名单 | `app/knowledge/provenance.py` | 只收 CNS、ACSM、NHS、Mayo、WHO、PubMed/PMC/ISSN 等机构站点 |
| 结构校验 | `tests/test_knowledge_sources.py`（8 例） | 字段齐全、id 唯一、content ≥20 字、https、域名在白名单 |
| 出处检查脚本 | `scripts/check_knowledge_sources.py` | 离线校验 + `--online` 逐个 URL 探活 |
| 引用闭环 | `today_tools.search_knowledge` → `today_agent.validate()` | 模型写的 `source_ids` 必须真实存在于库，否则结果作废降级 |
| 医疗边界 | `knowledge/service.py` | 红旗症状转"先就医"；用药/诊断类只给依据、明确拒答 |
| 无依据就拒答 | `service.ask()` | 检索不到返回"暂无足够依据"，不编 |

当前检查结果（离线）：

```
条目数：27
   8  www.nhs.uk
   8  www.cnsoc.org
   4  www.acsm.org
   4  www.mayoclinic.org
   2  www.who.int
   1  pmc.ncbi.nlm.nih.gov
离线检查通过（字段齐全 / id 唯一 / 出处为机构站点）
```

`--online` 探活（2026-11 实测）：NHS / WHO / CNS / PMC 返回 200；
ACSM 与 Mayo 返回 403 或连接超时（反爬与网络原因，不能据此判断链接失效）。

## 3. 刚刚修正的一处"引用错配"

原来的 `protein-basics` 把两条不同来源的结论塞进了一句话：

> 一般成年人 0.8–1.2 g/kg；力量训练/减脂 1.6–2.0 g/kg —— 出处：中国居民膳食指南

训练人群的 **1.4–2.0 g/kg** 来自 ISSN 的立场声明，不是膳食指南。已拆成两条：

| id | 内容 | 出处 |
|---|---|---|
| `protein-basics` | 一般成年人 0.8–1.2 g/kg | 中国居民膳食指南（2022） |
| `protein-training` | 力量训练 1.4–2.0 g/kg，减脂缺口大时接近上限 | [ISSN Position Stand: Protein and Exercise](https://pmc.ncbi.nlm.nih.gov/articles/PMC5477153/) |

实测问答已经能返回正确的引用：

```
问：力量训练蛋白质吃多少
答：有规律力量训练的人，每天约 1.4-2.0 g/公斤体重（按 88 kg 约 123-176 g）…
引用：[力量训练的人蛋白质吃多少 ← ISSN Position Stand]、[一般成年人蛋白质吃多少 ← 中国居民膳食指南]
```

## 4. 说清楚"不能保证什么"

- **没有逐条对照原文**：这些是我写的中文摘要，不是原文摘录；数字和表述可能被压缩得不够精确；
- **12 条只挂了机构首页**（脚本会给 warning），能证明机构存在，不能定位到具体章节；
- **没有记录指南版本/核对时间**：膳食指南有 2022 版、ISSN 有 2017 版，条目里没写"按哪版核对、何时核对"；
- **检索是 bigram 词面匹配**：同义表达（"膝盖不太行"）不一定命中；
- **没有医学/法务评审**：这不是面向患者的健康建议，只是运动参考。

## 5. 要真正上线还差什么

1. 每条结论拆到"句子级"，配页码/章节锚点（`source_url#section`）；
2. 数据模型加 `verified_at` / `guideline_version` / `evidence_level`；
3. 定期跑 `--online` + 人工抽检，指南更新时重建条目；
4. 高风险内容（疼痛、用药、疾病）只保留"就医"话术，不给具体判断；
5. 让懂运动医学的人做一轮评审，之后再谈"正确性"。

## 6. 怎么自己复核

```bash
cd backend
.venv/bin/python scripts/check_knowledge_sources.py           # 离线
.venv/bin/python scripts/check_knowledge_sources.py --online  # 联网探活
.venv/bin/python -m pytest tests/test_knowledge_sources.py -q # CI 口径
```
