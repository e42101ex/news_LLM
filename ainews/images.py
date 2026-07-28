"""為每個主題找一張縮圖。

來源優先順序：
1. RSS 本身帶的圖（media:thumbnail / media:content / enclosure / 內文第一張 img）
2. 文章頁的 og:image（RSS 沒帶圖時才去抓，實測 12/15 個來源有）

兩種輸出模式（config.toml 的 [images] mode）：
* "local"   —— 下載後裁成統一尺寸的 WebP 放進 docs/img/<日期>/，頁面自包含、
               存檔不會因為原站改版而破圖。代價是 repo 每天長一點。
* "hotlink" —— 直接連原站圖片，repo 不會長大，但存檔頁的圖日後可能失效。
"""

from __future__ import annotations

import hashlib
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import requests

if TYPE_CHECKING:      # 只用於型別註解；runtime 不 import，避免 fetch↔cluster 循環
    from .cluster import Topic

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

OG_PATTERNS = [
    r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
    r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)',
]
# 追蹤像素、佔位圖、logo 之類不值得當縮圖的東西
JUNK = re.compile(r"(1x1|pixel|spacer|blank|placeholder|avatar|logo|badge|button|"
                  r"feed-?icon|rss|gravatar|doubleclick|analytics)", re.I)
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", re.I)


@dataclass
class ImageConfig:
    mode: str = "local"          # local | hotlink | off
    width: int = 400
    height: int = 267            # 3:2，裁切成統一比例讓卡片整齊
    quality: int = 72
    og_fallback: bool = True
    keep_days: int = 60          # local 模式：清掉多少天前的圖片目錄
    timeout: int = 12
    workers: int = 8
    generic_threshold: int = 3   # 同一張圖被幾個主題共用就視為站台預設圖並丟棄

    @classmethod
    def from_config(cls, section: dict) -> ImageConfig:
        return cls(
            mode=section.get("mode", "local"),
            width=int(section.get("width", 400)),
            height=int(section.get("height", 267)),
            quality=int(section.get("quality", 72)),
            og_fallback=bool(section.get("og_fallback", True)),
            keep_days=int(section.get("keep_days", 60)),
            timeout=int(section.get("timeout", 12)),
            workers=int(section.get("workers", 8)),
            generic_threshold=int(section.get("generic_threshold", 3)),
        )


# ------------------------------------------------------------------ 從 RSS 取圖


def _plausible(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    return not JUNK.search(urlsplit(url).path)


def from_entry(entry, page_url: str) -> str:
    """從 feedparser 的 entry 抽出一張候選圖（找不到就回空字串）。"""
    candidates: list[str] = []

    for item in entry.get("media_thumbnail") or []:
        if item.get("url"):
            candidates.append(item["url"])
    for item in entry.get("media_content") or []:
        if item.get("url") and str(item.get("medium", "image")).startswith("image"):
            candidates.append(item["url"])
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and "image" in (link.get("type") or ""):
            if link.get("href"):
                candidates.append(link["href"])

    bodies = [c.get("value", "") for c in (entry.get("content") or [])]
    bodies.append(entry.get("summary") or entry.get("description") or "")
    for body in bodies:
        for match in re.finditer(r'<img[^>]+?src=["\']([^"\']+)["\']', body or "", re.I):
            candidates.append(match.group(1))

    for raw in candidates:
        url = urljoin(page_url, raw.strip())
        if _plausible(url):
            return url
    return ""


def resolve_og_image(page_url: str, timeout: int = 12) -> str:
    """抓文章頁的前段 HTML，找 og:image / twitter:image。"""
    try:
        response = requests.get(
            page_url,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"},
            timeout=timeout,
            stream=True,
        )
        if response.status_code != 200:
            return ""
        # <head> 通常在前 200KB 之內，不必下載整頁
        chunk = response.raw.read(200_000, decode_content=True) or b""
        response.close()
    except requests.RequestException:
        return ""

    html = chunk.decode("utf-8", "replace")
    for pattern in OG_PATTERNS:
        match = re.search(pattern, html, re.I)
        if match:
            url = urljoin(page_url, match.group(1).strip())
            if _plausible(url):
                return url
    return ""


# ------------------------------------------------------------- 下載並裁成縮圖


def _download_thumb(url: str, dest: Path, cfg: ImageConfig, referer: str = "",
                    attempt: int = 1) -> bool:
    from PIL import Image, ImageOps, UnidentifiedImageError

    headers = {"User-Agent": BROWSER_UA, "Accept": "image/*,*/*"}
    if referer:
        headers["Referer"] = referer          # 少數站台會擋沒有 Referer 的請求
    try:
        response = requests.get(
            url, headers=headers,
            # 第二次嘗試給更長的時間：動態縮圖端點（?resize=…）第一次命中常常很慢
            timeout=cfg.timeout * attempt,
        )
        if response.status_code != 200 or len(response.content) < 1024:
            return False
        with Image.open(BytesIO(response.content)) as im:
            im = ImageOps.exif_transpose(im)
            if im.width < 200 or im.height < 120:   # 太小的圖放大只會模糊
                return False
            im = im.convert("RGB")
            # 不放大：來源比目標小的話（例如 Google Trends 的 275x183 縮圖），
            # 按來源尺寸裁成同樣比例，避免放大變模糊
            tw, th = cfg.width, cfg.height
            if im.width < tw and im.height < th:
                scale = min(im.width / tw, im.height / th)
                tw, th = max(1, int(tw * scale)), max(1, int(th * scale))
            # fit＝等比縮放後居中裁切，卡片尺寸才會一致
            thumb = ImageOps.fit(im, (tw, th), Image.Resampling.LANCZOS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            thumb.save(dest, "WEBP", quality=cfg.quality, method=6)
        return True
    except (requests.RequestException, UnidentifiedImageError, OSError, ValueError):
        if attempt == 1:
            return _download_thumb(url, dest, cfg, referer, attempt=2)
        return False


def _prune(img_root: Path, keep_days: int) -> int:
    """刪掉過舊的日期目錄（HTML 存檔留著，圖片不必無限累積）。"""
    if not img_root.exists():
        return 0
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    for day_dir in img_root.iterdir():
        if day_dir.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_dir.name) and day_dir.name < cutoff:
            shutil.rmtree(day_dir, ignore_errors=True)
            removed += 1
    return removed


# ------------------------------------------------------------------ 主要進入點


def attach(topics: list["Topic"], cfg: ImageConfig, out_dir: Path, date: str) -> None:
    """就地為每個主題填上 topic.image（相對於 docs/ 的路徑，或遠端 URL）。"""
    if cfg.mode == "off" or not topics:
        return

    # 1) 先用 RSS 已經抓到的圖
    for topic in topics:
        for art in topic.articles:
            if art.image:
                topic.image = art.image
                break

    # 2) 沒圖的主題才去抓 og:image（每個主題只試最具代表性的前兩篇）
    missing = [t for t in topics if not t.image]
    if cfg.og_fallback and missing:
        print(f"  · {len(missing)} 個主題 RSS 沒帶圖，改抓 og:image…")

        def find(topic: "Topic") -> tuple["Topic", str]:
            for art in topic.articles[:2]:
                url = resolve_og_image(art.url, cfg.timeout)
                if url:
                    return topic, url
            return topic, ""

        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            for topic, url in pool.map(find, missing):
                topic.image = url

    # 3) 丟掉「站台預設分享圖」：同一張圖被多個主題共用，代表它不是該則新聞的圖
    #    （例如 36氪 的 og:image 是站台 logo，會讓好幾個主題掛同一張圖）
    counts: dict[str, int] = {}
    for topic in topics:
        if topic.image:
            counts[topic.image] = counts.get(topic.image, 0) + 1
    generic = {url for url, n in counts.items() if n >= cfg.generic_threshold}
    if generic:
        for topic in topics:
            if topic.image in generic:
                topic.image = ""
        print(f"  · 濾掉 {len(generic)} 張站台預設圖（被 {cfg.generic_threshold} 個以上主題共用）")

    found = sum(1 for t in topics if t.image)
    if cfg.mode == "hotlink":
        print(f"  · 縮圖：{found}/{len(topics)} 個主題有圖（hotlink，直接連原站）")
        return

    # 4) local 模式：下載＋裁切成統一尺寸的 WebP
    img_root = out_dir / "img"
    day_dir = img_root / date
    jobs = [(t, t.image) for t in topics if t.image]

    def work(job: tuple["Topic", str]) -> tuple["Topic", str]:
        topic, url = job
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".webp"
        dest = day_dir / name
        rel = f"img/{date}/{name}"
        referer = topic.articles[0].url if topic.articles else ""
        if dest.exists() or _download_thumb(url, dest, cfg, referer):
            return topic, rel
        return topic, ""

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        for topic, rel in pool.map(work, jobs):
            topic.image = rel

    ok = sum(1 for t in topics if t.image)
    size = sum(f.stat().st_size for f in day_dir.glob("*.webp")) if day_dir.exists() else 0
    print(f"  · 縮圖：{ok}/{len(topics)} 個主題有圖"
          f"（{found - ok} 張下載或解碼失敗，共 {size / 1024:.0f} KB）")

    pruned = _prune(img_root, cfg.keep_days)
    if pruned:
        print(f"  · 清掉 {pruned} 個超過 {cfg.keep_days} 天的圖片目錄")


def localize(urls: list[str], cfg: ImageConfig, out_dir: Path, date: str,
             ratio: tuple[int, int] | None = None) -> dict[str, str]:
    """把一批遠端圖片下載成縮圖，回傳 {原網址: docs 下的相對路徑}。

    給「社群熱門」頁用（Google Trends 的熱搜圖、Bluesky 貼文圖）。下載失敗的
    網址不會出現在回傳的 dict 裡，呼叫端就當成沒有圖處理。
    """
    if cfg.mode == "off" or not urls:
        return {}
    unique = [u for u in dict.fromkeys(urls) if u and u.startswith("http")]
    if cfg.mode == "hotlink":
        return {u: u for u in unique}

    shot = cfg
    if ratio:
        width = cfg.width
        shot = replace(cfg, width=width, height=max(1, round(width * ratio[1] / ratio[0])))

    day_dir = out_dir / "img" / date

    def work(url: str) -> tuple[str, str]:
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".webp"
        dest = day_dir / name
        if dest.exists() or _download_thumb(url, dest, shot):
            return url, f"img/{date}/{name}"
        return url, ""

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        done = dict(pool.map(work, unique))
    ok = {u: rel for u, rel in done.items() if rel}
    print(f"  · 社群圖片：{len(ok)}/{len(unique)} 張處理成功")
    return ok
