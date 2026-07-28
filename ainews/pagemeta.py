"""讀取文章頁的 metadata（標題／時間／摘要／圖）。

給沒有 RSS 的來源用：先抓列表頁的連結，再逐篇讀 <head> 裡的 Open Graph 與
JSON-LD 欄位。結果快取在 data/pagecache.json，所以每天只有新文章需要連線。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as date_parser

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEAD_BYTES = 200_000        # <head> 幾乎都在前 200KB 內，不必下載整頁

_META = r'<meta[^>]+{key}=["\']{name}["\'][^>]+content=["\']([^"\']*)'


def _meta(html: str, key: str, name: str) -> str:
    for pattern in (_META.format(key=key, name=re.escape(name)),
                    rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+{key}=["\']{re.escape(name)}["\']'):
        match = re.search(pattern, html, re.I)
        if match:
            return html_lib.unescape(match.group(1)).strip()
    return ""


def _jsonld_dates(html: str) -> list[str]:
    out: list[str] = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.I | re.S):
        for match in re.finditer(r'"date(?:Published|Created|Modified)"\s*:\s*"([^"]+)"', block):
            out.append(match.group(1))
    return out


def parse_datetime(raw: str, assume_tz: str = "Asia/Taipei") -> datetime | None:
    """把各種寫法（ISO、2026/07/28 10:39、含中文）轉成帶時區的 datetime。"""
    if not raw:
        return None
    cleaned = re.sub(r"[（(].*?[)）]", " ", raw).strip()
    cleaned = re.sub(r"\s*(上午|下午|AM|PM)\s*$", "", cleaned, flags=re.I)
    try:
        dt = date_parser.parse(cleaned, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:      # 沒寫時區的當地時間（台灣媒體幾乎都是）
        dt = dt.replace(tzinfo=ZoneInfo(assume_tz))
    return dt.astimezone(timezone.utc)


@dataclass
class PageMeta:
    url: str
    title: str = ""
    summary: str = ""
    image: str = ""
    published: str = ""        # ISO 8601 (UTC)

    def ok(self) -> bool:
        return bool(self.title and self.published)


def parse(html: str, url: str, assume_tz: str = "Asia/Taipei") -> PageMeta:
    title = (_meta(html, "property", "og:title")
             or _meta(html, "name", "twitter:title"))
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = html_lib.unescape(re.sub(r"\s+", " ", match.group(1))).strip() if match else ""
        title = re.sub(r"\s*[|｜\-–—]\s*[^|｜\-–—]{1,24}$", "", title)   # 去掉尾巴的站名

    summary = (_meta(html, "property", "og:description")
               or _meta(html, "name", "description")
               or _meta(html, "name", "twitter:description"))

    image = (_meta(html, "property", "og:image")
             or _meta(html, "property", "og:image:secure_url")
             or _meta(html, "name", "twitter:image"))
    if image:
        image = urljoin(url, image)

    raw_dates = [
        _meta(html, "property", "article:published_time"),
        _meta(html, "property", "article:modified_time"),
        _meta(html, "name", "pubdate"),
        _meta(html, "itemprop", "datePublished"),
        _meta(html, "name", "publish-date"),
        _meta(html, "name", "date"),
    ]
    raw_dates += _jsonld_dates(html)
    for match in re.finditer(r'<time[^>]+datetime=["\']([^"\']+)', html, re.I):
        raw_dates.append(match.group(1))

    published = ""
    for raw in raw_dates:
        dt = parse_datetime(raw, assume_tz)
        # 明顯不合理的時間（未來超過一天、或 2000 年以前）不要
        if dt and datetime(2000, 1, 1, tzinfo=timezone.utc) < dt < datetime.now(timezone.utc) + timedelta(days=1):
            published = dt.isoformat()
            break

    return PageMeta(url=url, title=title, summary=summary, image=image, published=published)


def fetch(url: str, timeout: int = 15, assume_tz: str = "Asia/Taipei") -> PageMeta | None:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=timeout,
            stream=True,
        )
        if response.status_code != 200:
            return None
        chunk = response.raw.read(HEAD_BYTES, decode_content=True) or b""
        response.close()
    except requests.RequestException:
        return None

    encoding = response.encoding or "utf-8"
    try:
        text = chunk.decode(encoding, "replace")
    except LookupError:
        text = chunk.decode("utf-8", "replace")
    return parse(text, url, assume_tz)


# ------------------------------------------------------------------------ 快取


class Cache:
    """url → metadata 的磁碟快取，避免每天重複抓同一批文章頁。"""

    def __init__(self, path: Path, keep_days: int = 21) -> None:
        self.path = path
        self.keep_days = keep_days
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}
        self.hits = 0
        self.misses = 0

    def get(self, url: str) -> PageMeta | None:
        entry = self.data.get(url)
        if not entry:
            return None
        self.hits += 1
        return PageMeta(url=url, title=entry.get("title", ""), summary=entry.get("summary", ""),
                        image=entry.get("image", ""), published=entry.get("published", ""))

    def put(self, meta: PageMeta) -> None:
        self.misses += 1
        self.data[meta.url] = {
            "title": meta.title, "summary": meta.summary,
            "image": meta.image, "published": meta.published,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }

    def save(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.keep_days)).isoformat()
        self.data = {
            url: entry for url, entry in self.data.items()
            if entry.get("cached_at", "") >= cutoff
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
