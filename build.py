#!/usr/bin/env python3
"""AI 每日新聞彙整 —— 抓取 → 分群 → （選用）LLM 摘要 → 產生 HTML。

常用指令：
    python build.py                      # 全流程（LLM 設定齊全就自動用）
    python build.py --llm off            # 純演算法，不呼叫任何 API
    python build.py --llm-test           # 只測試 LLM 端點通不通
    python build.py --stage collect      # 只抓取＋分群，結果寫到 data/latest.json
    python build.py --stage render       # 從 data/latest.json 重新產生 HTML

LLM 設定看 config.toml 的 [llm]；API key 請用環境變數或 .env：
    LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from ainews import cluster as clustering
from ainews import fetch, llm, render

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "latest.json"


def load_config(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def collect(cfg: dict, hours: int, threshold: float) -> list[clustering.Topic]:
    build = cfg.get("build", {})
    articles = fetch.fetch_all(cfg["sources"], hours=hours)
    if not articles:
        return []

    topics = clustering.cluster(articles, threshold=threshold)
    min_sources = int(build.get("min_cluster_sources", 1))
    if min_sources > 1:
        topics = [t for t in topics if len(t.sources) >= min_sources]
    max_topics = int(build.get("max_topics", 40))
    dropped = len(topics) - max_topics
    if dropped > 0:
        print(f"主題 {len(topics)} 個，取前 {max_topics} 個（略過 {dropped} 個較次要主題）")
        topics = topics[:max_topics]
    else:
        print(f"分群結果：{len(topics)} 個主題")
    return topics


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 每日新聞彙整")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--out", type=Path, default=ROOT / "docs", help="HTML 輸出目錄")
    parser.add_argument("--hours", type=int, help="覆寫收錄時間範圍（小時）")
    parser.add_argument("--similarity", type=float, help="覆寫分群門檻")
    parser.add_argument("--llm", choices=["auto", "on", "off"], default="auto",
                        help="是否用 LLM 產生中文摘要（auto＝設定齊全就用）")
    parser.add_argument("--llm-test", action="store_true",
                        help="只測試 LLM 連線（打一次最小請求後結束）")
    parser.add_argument("--base-url", help="覆寫 LLM base_url")
    parser.add_argument("--model", help="覆寫 LLM model id")
    parser.add_argument("--stage", choices=["all", "collect", "render"], default="all",
                        help="collect＝只抓取分群；render＝只從 data/latest.json 產生 HTML")
    parser.add_argument("--no-archive", action="store_true", help="不寫入日期存檔頁")
    args = parser.parse_args()

    llm.load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    build = cfg.get("build", {})
    llm_cfg = llm.LLMConfig.from_config(cfg.get("llm", {}))
    if args.base_url:
        llm_cfg.base_url = args.base_url
    if args.model:
        llm_cfg.model = args.model

    if args.llm_test:
        return 0 if llm.selftest(llm_cfg) else 1

    tz = build.get("timezone", "Asia/Taipei")
    site_title = build.get("site_title", "AI 每日新聞彙整")
    hours = args.hours or int(build.get("hours", 30))
    threshold = args.similarity or float(build.get("similarity", 0.26))

    if args.stage == "render":
        if not STATE.exists():
            print(f"找不到 {STATE}，請先跑 --stage collect", file=sys.stderr)
            return 1
        data = json.loads(STATE.read_text(encoding="utf-8"))
        topics = render.json_to_topics(data)
        hours = int(data.get("window_hours", hours))
    else:
        topics = collect(cfg, hours, threshold)

        use_llm = args.llm != "off" and llm.available(llm_cfg)
        if use_llm and topics:
            print(f"用 {llm_cfg.describe()} 整理主題摘要…")
            topics = llm.enrich(topics, llm_cfg)
        elif args.llm == "on":
            print(f"! 指定了 --llm on 但設定不完整（缺 {', '.join(llm_cfg.missing())}），"
                  f"改用演算法摘要", file=sys.stderr)
        elif args.llm == "auto" and args.stage != "render":
            print("（未使用 LLM 摘要，標題與摘要取自原文）")

        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(
            json.dumps(render.topics_to_json(topics, tz, hours), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"中繼資料 → {STATE.relative_to(ROOT)}")

    if args.stage == "collect":
        return 0

    index = render.render(
        topics, args.out,
        site_title=site_title, tz=tz, hours=hours, archive=not args.no_archive,
    )
    print(f"HTML → {index.relative_to(ROOT)}（{len(topics)} 個主題）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
