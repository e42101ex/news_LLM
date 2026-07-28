"""抓取 RSS 來源，過濾出「今天的 AI 新聞」。"""

from __future__ import annotations

import hashlib
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

CACHE_PATH = None      # 由 fetch_all 設定（data/pagecache.json）

import feedparser
import requests

from . import images, pagemeta

UA = "Mozilla/5.0 (compatible; auto-report-news/1.0; +https://github.com)"

# 命中任一關鍵字才算 AI 新聞（只對 ai_only = false 的綜合媒體生效）
AI_KEYWORDS = [
    "ai", "a.i.", "artificial intelligence", "machine learning", "deep learning",
    "neural network", "llm", "large language model", "generative", "genai",
    "transformer", "chatbot", "agentic", "ai agent", "copilot", "inference",
    "gpu", "tpu", "npu", "openai", "anthropic", "claude", "chatgpt", "gpt-",
    "gemini", "deepmind", "llama", "mistral", "grok", "xai", "midjourney",
    "stable diffusion", "hugging face", "nvidia", "cuda", "diffusion model",
    "rag", "vector database", "fine-tun", "multimodal", "sora", "perplexity",
    "人工智慧", "人工智能", "機器學習", "机器学习", "深度學習", "深度学习",
    "生成式", "大模型", "大語言模型", "大语言模型", "神經網路", "神经网络",
    "智慧體", "智能体", "算力", "晶片", "芯片", "推論", "推理", "模型",
]
# 這些詞單獨出現太容易誤判，需要另一個關鍵字陪襯
WEAK_KEYWORDS = {"ai", "a.i.", "模型", "推理", "推論", "晶片", "芯片", "gpu", "算力"}

_WORD_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


@dataclass
class Article:
    title: str
    url: str
    source: str
    lang: str
    published: str            # ISO 8601 (UTC)
    summary: str = ""
    image: str = ""           # RSS 帶的縮圖網址（沒有就留空，後續由 images.py 補 og:image）
    id: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    """去掉追蹤參數與 fragment，讓同一篇文章只出現一次。"""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = "&".join(
        kv for kv in parts.query.split("&")
        if kv and not kv.lower().startswith(("utm_", "fbclid", "gclid", "ref=", "source="))
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, query, ""))


def _matches_ai(text: str) -> bool:
    low = text.lower()
    hits: set[str] = set()
    for kw in AI_KEYWORDS:
        if kw.isascii() and re.fullmatch(r"[a-z0-9.\- ]+", kw):
            pattern = _WORD_BOUNDARY_CACHE.get(kw)
            if pattern is None:
                pattern = re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])")
                _WORD_BOUNDARY_CACHE[kw] = pattern
            found = pattern.search(low) is not None
        else:
            found = kw in low
        if found:
            hits.add(kw)
    if not hits:
        return False
    return bool(hits - WEAK_KEYWORDS) or len(hits) >= 2


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    return None


def _fetch_rss(source: dict, cutoff: datetime, timeout: int = 20) -> list[Article]:
    # 自己用 requests 抓（feedparser 內建的下載沒有 timeout，會拖垮排程工作）
    try:
        response = requests.get(
            source["url"],
            headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        print(f"  ! {source['name']}: 連線失敗 ({type(exc).__name__})")
        return []

    if response.status_code != 200:
        print(f"  ! {source['name']}: HTTP {response.status_code}")
        return []

    feed = feedparser.parse(response.content)
    if not feed.entries:
        reason = getattr(feed, "bozo_exception", "沒有項目")
        print(f"  ! {source['name']}: 讀不到文章 ({reason})")
        return []

    out: list[Article] = []
    for entry in feed.entries:
        title = _clean(entry.get("title"))
        url = canonical_url(entry.get("link") or "")
        if not title or not url:
            continue

        published = _entry_time(entry)
        if published is None:
            # 沒有時間戳的來源（少數 blog）一律當成「剛剛」，靠 URL 去重避免重複收錄
            published = datetime.now(tz=timezone.utc)
        if published < cutoff:
            continue

        summary = _clean(entry.get("summary") or entry.get("description"))[:600]
        if not source.get("ai_only") and not _matches_ai(f"{title} {summary}"):
            continue

        out.append(Article(
            title=title,
            url=url,
            source=source["name"],
            lang=source.get("lang", "en"),
            published=published.isoformat(),
            summary=summary,
            image=images.from_entry(entry, url),
            id=hashlib.sha1(url.encode()).hexdigest()[:12],
            tags=[t.get("term", "") for t in entry.get("tags", []) if t.get("term")][:5],
        ))
    print(f"  · {source['name']}: {len(out)} 篇")
    return out


def _listing_links(source: dict, timeout: int = 20) -> list[str]:
    """抓列表頁，取出符合 link_pattern 的文章連結。"""
    try:
        response = requests.get(
            source["url"],
            headers={"User-Agent": pagemeta.BROWSER_UA, "Accept": "text/html,*/*"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        print(f"  ! {source['name']}: 連線失敗 ({type(exc).__name__})")
        return []
    if response.status_code != 200:
        print(f"  ! {source['name']}: HTTP {response.status_code}")
        return []

    pattern = re.compile(source.get("link_pattern", r"/\d{4,}"))
    links: list[str] = []
    for raw in re.findall(r'href=["\']([^"\'#]+)', response.text):
        url = canonical_url(urljoin(source["url"], html.unescape(raw)))
        if url and pattern.search(url) and url not in links:
            links.append(url)
    return links[:int(source.get("max_items", 25))]


def _fetch_html(source: dict, cutoff: datetime, cache: pagemeta.Cache | None,
                tz: str = "Asia/Taipei") -> list[Article]:
    """沒有 RSS 的站台：列表頁取連結 → 逐篇讀 og / JSON-LD metadata。"""
    links = _listing_links(source)
    if not links:
        return []

    def load(url: str) -> pagemeta.PageMeta | None:
        if cache is not None:
            cached = cache.get(url)
            if cached is not None:
                return cached
        meta = pagemeta.fetch(url, assume_tz=tz)
        if meta and meta.ok() and cache is not None:
            cache.put(meta)
        return meta

    with ThreadPoolExecutor(max_workers=6) as pool:
        metas = list(pool.map(load, links))

    out: list[Article] = []
    for meta in metas:
        if not meta or not meta.ok():
            continue
        published = datetime.fromisoformat(meta.published)
        if published < cutoff:
            continue
        summary = meta.summary[:600]
        if not source.get("ai_only") and not _matches_ai(f"{meta.title} {summary}"):
            continue
        out.append(Article(
            title=meta.title, url=meta.url, source=source["name"],
            lang=source.get("lang", "zh"), published=published.isoformat(),
            summary=summary, image=meta.image,
            id=hashlib.sha1(meta.url.encode()).hexdigest()[:12],
        ))
    print(f"  · {source['name']}: {len(out)} 篇（掃 {len(links)} 個連結）")
    return out


def _fetch_wp_json(source: dict, cutoff: datetime, tz: str = "Asia/Taipei") -> list[Article]:
    """WordPress REST API（/wp-json/wp/v2/posts）—— 給 RSS 被擋掉的 WP 站用。"""
    params = {"per_page": int(source.get("max_items", 20)), "_embed": "1"}
    try:
        response = requests.get(
            source["url"], params=params,
            headers={"User-Agent": pagemeta.BROWSER_UA, "Accept": "application/json"},
            timeout=20,
        )
        if response.status_code != 200:
            print(f"  ! {source['name']}: HTTP {response.status_code}")
            return []
        posts = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  ! {source['name']}: {type(exc).__name__}")
        return []

    out: list[Article] = []
    for post in posts if isinstance(posts, list) else []:
        title = _clean(str((post.get("title") or {}).get("rendered", "")))
        url = canonical_url(post.get("link") or "")
        if not title or not url:
            continue
        published = pagemeta.parse_datetime(post.get("date_gmt") or post.get("date") or "", "UTC"
                                            if post.get("date_gmt") else tz)
        if not published or published < cutoff:
            continue
        summary = _clean(str((post.get("excerpt") or {}).get("rendered", "")))[:600]
        if not source.get("ai_only") and not _matches_ai(f"{title} {summary}"):
            continue
        media = (post.get("_embedded", {}).get("wp:featuredmedia") or [{}])[0]
        out.append(Article(
            title=title, url=url, source=source["name"],
            lang=source.get("lang", "zh"), published=published.isoformat(),
            summary=summary, image=str(media.get("source_url") or ""),
            id=hashlib.sha1(url.encode()).hexdigest()[:12],
        ))
    print(f"  · {source['name']}: {len(out)} 篇（WP API 回 {len(posts) if isinstance(posts, list) else 0} 篇）")
    return out


def _normalize_title(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", title.lower())


def fetch_all(sources: list[dict], hours: int = 30) -> list[Article]:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    print(f"抓取 {len(sources)} 個來源（{hours} 小時內）…")

    cache = pagemeta.Cache(CACHE_PATH) if CACHE_PATH else None

    def dispatch(source: dict) -> list[Article]:
        kind = source.get("type", "rss")
        if kind == "html":
            return _fetch_html(source, cutoff, cache)
        if kind == "wp_json":
            return _fetch_wp_json(source, cutoff)
        return _fetch_rss(source, cutoff)

    with ThreadPoolExecutor(max_workers=min(12, len(sources) or 1)) as pool:
        results = list(pool.map(dispatch, sources))

    if cache is not None:
        cache.save()
        if cache.hits or cache.misses:
            print(f"  · 文章頁快取：命中 {cache.hits}、新抓 {cache.misses}")

    seen: set[str] = set()
    seen_titles: set[str] = set()
    articles: list[Article] = []
    for group in results:
        for art in group:
            key = _normalize_title(art.title)
            # 同一篇文章有時會有兩種網址（例如 DIGITIMES 的不同 query），用標題再擋一次
            if art.url in seen or (key and key in seen_titles):
                continue
            seen.add(art.url)
            seen_titles.add(key)
            articles.append(art)

    articles.sort(key=lambda a: a.published, reverse=True)
    print(f"合計 {len(articles)} 篇（去重後）")
    return articles
