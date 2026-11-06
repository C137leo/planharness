"""生成 60 条人工选定目标条目的评测集（data/rag/eval_queries.json）。

为什么不用"标题包含关键词"当判定：
那个口径太宽松，检索出一条标题里恰好含"深蹲"的冷门动作也算命中。
这里由人（我）为每条查询指定**应该被检索到的条目**（按标题锁定），
脚本只负责把标题解析成 chunk id，评测时按 id 精确判分。
"""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data" / "rag"
CHUNKS = DATA / "chunks.jsonl"
OUT = DATA / "eval_queries.json"

# (类别, 查询, [期望标题片段...])  —— 人工判定：这些才是"正确结果"
CASES: list[tuple[str, str, list[str]]] = [
    # ---- 字面查询：动作 ----
    ("literal", "深蹲动作要领", ["杠铃深蹲（Barbell Squat）"]),
    ("literal", "高脚杯深蹲怎么做", ["高脚杯深蹲（Goblet Squat）"]),
    ("literal", "杠铃卧推", ["杠铃卧推（Barbell Bench Press"]),
    ("literal", "哑铃卧推", ["哑铃卧推（Dumbbell Bench Press）"]),
    ("literal", "上斜卧推", ["上斜杠铃卧推", "上斜哑铃卧推"]),
    ("literal", "硬拉动作要领", ["杠铃硬拉（Barbell Deadlift）"]),
    ("literal", "罗马尼亚硬拉", ["罗马尼亚硬拉"]),
    ("literal", "杠铃俯身划船", ["杠铃俯身划船"]),
    ("literal", "坐姿绳索划船", ["坐姿绳索划船"]),
    ("literal", "高位下拉", ["宽握高位下拉"]),
    ("literal", "引体向上", ["引体向上（Pullups）", "反手引体向上"]),
    ("literal", "杠铃肩推", ["杠铃肩推"]),
    ("literal", "哑铃肩推", ["哑铃肩推"]),
    ("literal", "哑铃侧平举", ["哑铃侧平举"]),
    ("literal", "面拉", ["面拉"]),
    ("literal", "杠铃弯举", ["杠铃弯举"]),
    ("literal", "锤式弯举", ["锤式弯举"]),
    ("literal", "三头下压", ["三头下压"]),
    ("literal", "凳上臂屈伸", ["凳上臂屈伸"]),
    ("literal", "腿举", ["腿举"]),
    ("literal", "腿屈伸", ["腿屈伸"]),
    ("literal", "俯卧腿弯举", ["俯卧腿弯举"]),
    ("literal", "站姿提踵", ["站姿提踵"]),
    ("literal", "杠铃臀桥", ["杠铃臀桥"]),
    ("literal", "杠铃臀推", ["杠铃臀推"]),
    ("literal", "杠铃弓步", ["杠铃弓步"]),
    ("literal", "平板支撑", ["平板支撑"]),
    ("literal", "卷腹", ["卷腹"]),
    ("literal", "侧支撑", ["侧支撑"]),
    ("literal", "悬垂举腿", ["悬垂举腿"]),
    # ---- 口语/同义转述：动作目标 ----
    ("paraphrase", "想练胸", ["杠铃卧推", "哑铃卧推", "俯卧撑", "哑铃飞鸟"]),
    ("paraphrase", "练背的动作", ["杠铃俯身划船", "引体", "高位下拉"]),
    ("paraphrase", "下肢力量练什么",
     ["杠铃深蹲", "腿举", "杠铃弓步", "哑铃弓步", "俯卧腿弯举", "腿屈伸"]),
    ("paraphrase", "臀部塑形", ["杠铃臀推", "杠铃臀桥"]),
    ("paraphrase", "肩膀怎么练", ["杠铃肩推", "哑铃肩推", "哑铃侧平举"]),
    ("paraphrase", "手臂线条怎么练", ["杠铃弯举", "三头下压", "哑铃弯举"]),
    ("paraphrase", "核心怎么练", ["平板支撑", "卷腹", "侧支撑"]),
    ("paraphrase", "大腿前侧练什么", ["腿屈伸", "杠铃深蹲", "腿举"]),
    ("paraphrase", "大腿后侧练什么", ["俯卧腿弯举", "罗马尼亚硬拉", "硬拉"]),
    ("paraphrase", "小腿怎么练", ["提踵"]),
    ("paraphrase", "推胸的动作", ["杠铃卧推", "哑铃卧推", "上斜"]),
    ("paraphrase", "拉背的动作", ["高位下拉", "划船", "引体"]),
    ("paraphrase", "减脂期做什么力量动作",
     ["杠铃深蹲", "杠铃硬拉", "杠铃卧推", "杠铃俯身划船", "杠铃肩推", "罗马尼亚硬拉"]),
    ("paraphrase", "新手先练什么动作",
     ["杠铃深蹲", "杠铃卧推", "杠铃硬拉", "俯卧撑", "杠铃俯身划船", "平板支撑"]),
    ("paraphrase", "居家无器械练胸", ["俯卧撑"]),
    # ---- 食物营养 ----
    ("food", "鸡胸肉蛋白质", ["鸡胸肉"]),
    ("food", "鸡蛋蛋白质", ["鸡蛋", "全蛋"]),
    ("food", "牛奶蛋白质", ["牛奶"]),
    ("food", "三文鱼脂肪", ["三文鱼"]),
    ("food", "牛肉热量", ["牛里脊", "牛肉"]),
    ("food", "燕麦碳水", ["燕麦"]),
    ("food", "糙米热量", ["糙米"]),
    ("food", "红薯碳水", ["红薯"]),
    ("food", "西兰花营养", ["西兰花"]),
    ("food", "牛油果脂肪", ["牛油果"]),
    ("food", "香蕉热量", ["香蕉"]),
    ("food", "苹果碳水", ["苹果"]),
    ("food", "杏仁脂肪", ["杏仁"]),
    ("food", "花生酱蛋白质", ["花生酱"]),
    ("food", "橄榄油脂肪", ["橄榄油"]),
]


def main() -> None:
    chunks = [json.loads(line) for line in CHUNKS.read_text(encoding="utf-8").splitlines() if line]
    by_title = [(item["id"], item["title"]) for item in chunks]
    cases: list[dict] = []
    unresolved: list[str] = []
    for category, query, snippets in CASES:
        ids: list[str] = []
        for snippet in snippets:
            matched = [cid for cid, title in by_title if snippet in title]
            if not matched:
                unresolved.append(f"{query} -> {snippet}")
            ids.extend(matched)
        if ids:
            cases.append({"category": category, "query": query,
                          "expected_ids": sorted(set(ids)), "targets": snippets})
    if unresolved:
        print("以下目标条目在语料里找不到，需要检查：")
        for line in unresolved:
            print("  -", line)
    OUT.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {len(cases)} 条评测（literal/paraphrase/food），-> {OUT}")


if __name__ == "__main__":
    main()
