#!/usr/bin/env python3
"""人工／Claude 手動微調 data/latest.json 的小工具。

在 Claude Cowork 裡的用途：先跑 `build.py --stage collect --llm off` 抓好資料，
再用這支指令改標題摘要、合併漏抓的重複主題，最後跑 `build.py --stage render`。

    python curate.py list
    python curate.py merge t0003 t0011          # 把 t0011 併進 t0003
    python curate.py set t0003 --title "…" --summary "…" --category 企業與資金 --importance 5
    python curate.py drop t0020                 # 移除不相關的主題
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "latest.json"
CATEGORIES = [
    "模型與研究", "產品與應用", "企業與資金", "晶片與基礎設施",
    "政策與法規", "安全與倫理", "開源社群", "其他",
]


def load() -> dict:
    if not STATE.exists():
        sys.exit(f"找不到 {STATE}，請先執行：python build.py --stage collect")
    return json.loads(STATE.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    data["topic_count"] = len(data["topics"])
    data["article_count"] = sum(len(t["articles"]) for t in data["topics"])
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def find(data: dict, key: str) -> dict:
    for topic in data["topics"]:
        if topic["key"] == key:
            return topic
    sys.exit(f"找不到主題 {key}（用 `python curate.py list` 看目前的 key）")


def cmd_list(data: dict, args) -> None:
    for topic in data["topics"]:
        flag = "★" if topic.get("importance", 3) >= 4 else " "
        print(f'{flag} {topic["key"]}  [{len(topic["articles"])}篇/{len(topic["sources"])}家] '
              f'{topic.get("category", "其他")}  {topic["title"][:52]}')
        if args.verbose:
            for art in topic["articles"]:
                print(f'      · {art["source"]}: {art["title"][:66]}')
    print(f'\n共 {len(data["topics"])} 個主題 / {sum(len(t["articles"]) for t in data["topics"])} 篇報導')


def cmd_merge(data: dict, args) -> None:
    host = find(data, args.keys[0])
    seen = {a["url"] for a in host["articles"]}
    for key in args.keys[1:]:
        guest = find(data, key)
        if guest is host:
            continue
        host["articles"].extend(a for a in guest["articles"] if a["url"] not in seen)
        seen.update(a["url"] for a in guest["articles"])
        host["importance"] = max(host.get("importance", 3), guest.get("importance", 3))
        data["topics"].remove(guest)
        print(f'{key} → {host["key"]}')
    host["articles"].sort(key=lambda a: a["published"], reverse=True)
    host["sources"] = list(dict.fromkeys(a["source"] for a in host["articles"]))
    save(data)
    print(f'{host["key"]} 現在有 {len(host["articles"])} 篇、{len(host["sources"])} 家來源')


def cmd_set(data: dict, args) -> None:
    topic = find(data, args.key)
    for attr in ("title", "summary", "category"):
        value = getattr(args, attr)
        if value:
            topic[attr] = value
    if args.importance:
        topic["importance"] = args.importance
    topic["llm_enriched"] = True
    save(data)
    print(f'{topic["key"]} 已更新：{topic["title"][:60]}')


def cmd_drop(data: dict, args) -> None:
    for key in args.keys:
        data["topics"].remove(find(data, key))
        print(f"移除 {key}")
    save(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="微調 data/latest.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出所有主題")
    p_list.add_argument("-v", "--verbose", action="store_true", help="連原文標題一起列")
    p_list.set_defaults(func=cmd_list)

    p_merge = sub.add_parser("merge", help="合併主題（第一個 key 為保留者）")
    p_merge.add_argument("keys", nargs="+")
    p_merge.set_defaults(func=cmd_merge)

    p_set = sub.add_parser("set", help="改寫某個主題的標題／摘要／分類／重要度")
    p_set.add_argument("key")
    p_set.add_argument("--title")
    p_set.add_argument("--summary")
    p_set.add_argument("--category", choices=CATEGORIES)
    p_set.add_argument("--importance", type=int, choices=[1, 2, 3, 4, 5])
    p_set.set_defaults(func=cmd_set)

    p_drop = sub.add_parser("drop", help="移除主題")
    p_drop.add_argument("keys", nargs="+")
    p_drop.set_defaults(func=cmd_drop)

    args = parser.parse_args()
    args.func(load(), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
