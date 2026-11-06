"""三种检索引擎的对比评测（keyword / vector / hybrid）。

用法：
    cd backend && .venv/bin/python scripts/eval_rag.py

口径：
- 15 条中文查询，每条给一组"期望标题关键词"；
- 命中 = top-k 里任意一条标题包含任一期望关键词；
- 输出 hit@1 / hit@3 与平均名次。

这套评测很小，但足够回答"换不换检索、值不值得上真 embedding"。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge import rag  # noqa: E402

CASES: list[tuple[str, list[str]]] = [
    ("深蹲动作要领", ["杠铃深蹲", "高脚杯深蹲"]),
    ("卧推怎么练", ["杠铃卧推", "哑铃卧推"]),
    ("硬拉姿势", ["杠铃硬拉", "罗马尼亚硬拉"]),
    ("高脚杯深蹲", ["高脚杯深蹲"]),
    ("肩推动作", ["杠铃肩推", "哑铃肩推"]),
    ("侧平举", ["侧平举"]),
    ("划船练背", ["划船"]),
    ("臀桥", ["臀桥"]),
    ("平板支撑", ["平板支撑"]),
    ("鸡胸肉每100克蛋白质", ["鸡胸肉"]),
    ("糙米热量", ["糙米"]),
    ("燕麦碳水", ["燕麦"]),
    ("西兰花营养", ["西兰花"]),
    ("牛油果脂肪", ["牛油果"]),
    ("希腊酸奶蛋白质", ["希腊酸奶"]),
    # 口语/同义转述：词面匹配会开始吃力，真语义向量才有优势
    ("胸肌怎么练", ["卧推", "飞鸟", "俯卧撑", "夹胸"]),
    ("下肢力量练什么", ["深蹲", "弓步", "腿举"]),
    ("臀部塑形动作", ["臀桥", "臀推"]),
    ("蛋白质高的肉类", ["鸡胸肉", "牛肉", "三文鱼", "虾", "金枪鱼"]),
    ("减脂期主食选什么", ["糙米", "燕麦", "红薯", "土豆"]),
    ("鸡蛋蛋白质", ["鸡蛋", "蛋"]),
    ("米饭热量", ["米饭", "米"]),
    ("西兰花营养", ["西兰花"]),
    ("牛奶蛋白质", ["牛奶"]),
    ("红薯碳水", ["红薯"]),
    ("香蕉热量", ["香蕉"]),
    ("三文鱼蛋白质", ["三文鱼"]),
    ("卷腹怎么练", ["卷腹", "Crunch"]),
    ("平板支撑练核心", ["平板支撑", "Plank"]),
    ("哑铃卧推", ["哑铃卧推"]),
    ("引体向上", ["引体"]),
    ("臀推动作", ["臀推"]),
]


def evaluate(engine: str, k: int = 3) -> tuple[float, float]:
    hit1 = hit3 = 0
    for query, expects in CASES:
        hits = rag.search(query, engine=engine, limit=k)
        titles = [hit["title"] for hit in hits]
        ranks = [index + 1 for index, title in enumerate(titles)
                 if any(keyword in title for keyword in expects)]
        if ranks:
            hit3 += 1
            if ranks[0] == 1:
                hit1 += 1
    total = len(CASES)
    return round(hit1 / total, 3), round(hit3 / total, 3)


def main() -> None:
    import time

    print("语料：", rag.stats())
    start = time.perf_counter()
    rag.search("深蹲动作要领", engine="hybrid", limit=5)
    print(f"首次检索（含建索引）：{int((time.perf_counter() - start) * 1000)} ms")
    print(f"{'引擎':<10}{'hit@1':>8}{'hit@3':>8}")
    for engine in ("keyword", "vector", "hybrid"):
        one, three = evaluate(engine)
        print(f"{engine:<10}{one:>8.0%}{three:>8.0%}")
    print("\n未命中明细（hybrid）：")
    for query, expects in CASES:
        hits = rag.search(query, engine="hybrid", limit=3)
        if not any(any(k in hit["title"] for k in expects) for hit in hits):
            print(f"  ✗ {query} 期望 {expects} 实际 {[hit['title'] for hit in hits]}")
    print("\n示例（深蹲动作要领 / hybrid）：")
    for hit in rag.search("深蹲动作要领", engine="hybrid", limit=3):
        print(f"  {hit['score']:.4f}  {hit['title']}  [{hit['source_title'][:18]}]")


if __name__ == "__main__":
    main()
