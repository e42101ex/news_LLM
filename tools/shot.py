#!/usr/bin/env python3
"""用 Playwright 對產出的頁面截圖，確認版面沒跑掉。

    python tools/shot.py                      # 桌機淺色 + 桌機深色 + 手機，各截頂部
    python tools/shot.py --full                # 整頁
    python tools/shot.py --url https://…       # 截線上版本
    python tools/shot.py --only mobile-dark

截圖放在 .shots/（已被 .gitignore 排除）。

為什麼需要這支腳本：Firefox 的 `--screenshot` 不等圖片解碼，頁面上有幾十張
縮圖時會全部拍成灰色佔位框，看不出真正的版面。這裡會先滾一遍觸發 lazy
loading，再等到所有 <img> 都 decode 完才截圖。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".shots"

# name -> (寬, 高, 深色?)
VIEWS = {
    "desktop-light": (1280, 900, False),
    "desktop-dark": (1280, 900, True),
    "mobile-light": (390, 844, False),
    "mobile-dark": (390, 844, True),
}

# 滾一遍觸發 lazy loading，再等所有圖片 decode 完（含失敗的也要結束等待）
SETTLE = """
async () => {
  const step = Math.round(window.innerHeight * 0.8);
  for (let y = 0; y < document.body.scrollHeight; y += step) {
    window.scrollTo(0, y);
    await new Promise(r => requestAnimationFrame(() => setTimeout(r, 60)));
  }
  window.scrollTo(0, 0);
  await Promise.all(Array.from(document.images).map(img =>
    img.complete
      ? (img.decode ? img.decode().catch(() => {}) : Promise.resolve())
      : new Promise(res => { img.addEventListener('load', res, {once:true});
                             img.addEventListener('error', res, {once:true}); })
  ));
  await new Promise(r => setTimeout(r, 150));
  return {
    images: document.images.length,
    broken: Array.from(document.images).filter(i => !i.complete || i.naturalWidth === 0).length,
  };
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="頁面截圖")
    parser.add_argument("--url", help="要截的網址（預設用本機 docs/index.html）")
    parser.add_argument("--full", action="store_true", help="截整頁而非只截頂部")
    parser.add_argument("--only", choices=sorted(VIEWS), action="append",
                        help="只截指定的版型（可重複）")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("需要 playwright：\n"
                 f"  {sys.executable} -m pip install playwright\n"
                 f"  {sys.executable} -m playwright install chromium")

    target = args.url or (ROOT / "docs" / "index.html").as_uri()
    views = {k: VIEWS[k] for k in (args.only or VIEWS)}
    OUT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        # channel='chromium' 用完整 Chromium，省下另外下載 headless shell
        browser = pw.chromium.launch(channel="chromium")
        for name, (width, height, dark) in views.items():
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
                color_scheme="dark" if dark else "light",
                is_mobile=width < 500,
                has_touch=width < 500,
            )
            page = context.new_page()
            page.goto(target, wait_until="load", timeout=60_000)
            stats = page.evaluate(SETTLE)
            path = OUT_DIR / f"{name}.png"
            page.screenshot(path=str(path), full_page=args.full)
            note = f"，{stats['broken']} 張圖沒載入" if stats["broken"] else "，圖片全部載入"
            print(f"  {path.relative_to(ROOT)}  {width}x{height}"
                  f"{' 深色' if dark else ' 淺色'}  {stats['images']} 張圖{note}")
            context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
