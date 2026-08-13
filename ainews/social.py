"""「社群熱門」分頁的資料來源。

三個區塊，都不需要任何 API key：

1. 台灣熱搜（Google Trends RSS）—— 每個熱搜附熱度值與最多 3 則相關新聞（含來源與圖）
2. Bluesky 熱門話題（app.bsky.unspecced.getTrends）—— 話題名稱與貼文數
3. Bluesky AI 熱門貼文（app.bsky.feed.searchPosts）—— 依讚數排序，過濾雜訊

為什麼不是 Threads：官方 API 沒有 trending 端點，且 insights（讚／回覆數）
只能讀「自己的貼文」，拿不到別人貼文的熱度，無法排序；網頁端是純 JS 殼、
無伺服器端渲染內容，自動化抓取也違反 Meta 服務條款。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.parse
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (compatible; auto-report-news/1.0)"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
TRENDS_RSS = "https://trends.google.com/trending/rss"
# 注意：public.api.bsky.app 在台灣會被 CDN 擋 403，要用 api.bsky.app
BSKY = "https://api.bsky.app/xrpc"

# 貼文要命中至少一個「強訊號」才算 AI 相關（避免 Claude Monet、AI 繪圖標籤之類的誤判）
STRONG = [
    "openai", "anthropic", "chatgpt", "gpt-4", "gpt-5", "claude code", "claude ai",
    "gemini", "deepmind", "llm", "large language model", "machine learning",
    "deep learning", "neural net", "ai agent", "agentic", "genai", "generative ai",
    "diffusion model", "hugging face", "transformer model", "fine-tuning", "inference",
    "nvidia", "deepseek", "mistral", "llama 3", "llama 4", "copilot",
    "人工智慧", "人工智能", "生成式", "大語言模型", "大语言模型", "機器學習", "机器学习",
]
# 明顯是 AI 繪圖／成人內容洗版的標籤
SPAM = re.compile(r"(aiイラスト|ai美女|aigirls|aiart|ai_art|aigraphics|nsfw|"
                  r"onlyfans|promo|discount code|airdrop|giveaway)", re.I)


_STRONG_RE = re.compile(
    "|".join(
        # ASCII 詞彙用詞界比對，避免 "llm" 命中別的單字內部；中文直接子字串
        rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])" if t.isascii() else re.escape(t)
        for t in STRONG
    ),
    re.I,
)


def _matches_strong(text: str) -> bool:
    return _STRONG_RE.search(text) is not None


# ------------------------------------------------------------------ 資料結構


@dataclass
class NewsRef:
    title: str
    url: str
    source: str
    image: str = ""


@dataclass
class Trend:
    title: str
    traffic: str = ""
    url: str = ""
    image: str = ""
    published: str = ""
    news: list[NewsRef] = field(default_factory=list)


@dataclass
class BskyTrend:
    topic: str
    post_count: int = 0
    status: str = ""
    started_at: str = ""
    url: str = ""


@dataclass
class BskyPost:
    text: str
    handle: str
    display_name: str = ""
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    created_at: str = ""
    url: str = ""
    image: str = ""
    external: str = ""
    query: str = ""


@dataclass
class HotArticle:
    """網路溫度計（dailyview）的熱門話題文章。"""
    title: str
    url: str
    summary: str = ""
    category: str = ""
    date: str = ""
    image: str = ""


@dataclass
class RankItem:
    title: str
    url: str
    period: str = ""      # 觀測時間：2026.07.20-2026.07.26
    image: str = ""


@dataclass
class RankBoard:
    """Social Lab 的單一平台排行榜（每週一篇彙整文章）。"""
    platform: str
    url: str
    items: list[RankItem] = field(default_factory=list)


@dataclass
class SocialDigest:
    date: str
    generated_at: str
    timezone: str
    window_hours: int
    trends: list[Trend] = field(default_factory=list)
    bsky_trends: list[BskyTrend] = field(default_factory=list)
    bsky_posts: list[BskyPost] = field(default_factory=list)
    dailyview: list[HotArticle] = field(default_factory=list)
    boards: list[RankBoard] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SocialDigest:
        return cls(
            date=data["date"], generated_at=data["generated_at"],
            timezone=data.get("timezone", "Asia/Taipei"),
            window_hours=int(data.get("window_hours", 30)),
            trends=[Trend(**{**t, "news": [NewsRef(**n) for n in t.get("news", [])]})
                    for t in data.get("trends", [])],
            bsky_trends=[BskyTrend(**t) for t in data.get("bsky_trends", [])],
            bsky_posts=[BskyPost(**p) for p in data.get("bsky_posts", [])],
            dailyview=[HotArticle(**a) for a in data.get("dailyview", [])],
            boards=[RankBoard(platform=b["platform"], url=b.get("url", ""),
                              items=[RankItem(**i) for i in b.get("items", [])])
                    for b in data.get("boards", [])],
        )

    @property
    def total(self) -> int:
        return (len(self.trends) + len(self.bsky_trends) + len(self.bsky_posts)
                + len(self.dailyview) + sum(len(b.items) for b in self.boards))


# --------------------------------------------------------- 1) Google Trends


def _tag(block: str, name: str) -> str:
    match = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", block, re.S)
    return html_lib.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else ""


def _tidy_title(title: str) -> str:
    """Google Trends 會在中文詞之間插空格（「違約 交割」），去掉它。"""
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", title).strip()


def fetch_google_trends(geo: str = "TW", limit: int = 10, timeout: int = 25) -> list[Trend]:
    try:
        response = requests.get(TRENDS_RSS, params={"geo": geo},
                                headers={"User-Agent": UA}, timeout=timeout)
        if response.status_code != 200:
            print(f"  ! Google Trends: HTTP {response.status_code}")
            return []
    except requests.RequestException as exc:
        print(f"  ! Google Trends: {type(exc).__name__}")
        return []

    out: list[Trend] = []
    for block in response.text.split("<item>")[1:]:
        title = _tidy_title(_tag(block, "title"))
        if not title:
            continue
        news: list[NewsRef] = []
        for item in re.findall(r"<ht:news_item>(.*?)</ht:news_item>", block, re.S):
            url = _tag(item, "ht:news_item_url")
            if not url:
                continue
            news.append(NewsRef(
                title=_tag(item, "ht:news_item_title"),
                url=url,
                source=_tag(item, "ht:news_item_source"),
                image=_tag(item, "ht:news_item_picture"),
            ))
        image = _tag(block, "ht:picture")
        if image.startswith("//"):
            image = "https:" + image
        out.append(Trend(
            title=title,
            traffic=_tag(block, "ht:approx_traffic"),
            url=_tag(block, "link") or
                "https://trends.google.com/trends/explore?" +
                urllib.parse.urlencode({"q": title, "geo": geo}),
            image=image,
            published=_tag(block, "pubDate"),
            news=news[:3],
        ))
        if len(out) >= limit:
            break
    print(f"  · Google Trends（{geo}）：{len(out)} 個熱搜")
    return out


# ------------------------------------------------------------- 2/3) Bluesky


def _bsky(path: str, timeout: int = 25, **params) -> dict | None:
    url = f"{BSKY}/{path}?" + urllib.parse.urlencode(params)
    try:
        response = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if response.status_code != 200:
            print(f"  ! Bluesky {path}: HTTP {response.status_code}")
            return None
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  ! Bluesky {path}: {type(exc).__name__}")
        return None


def fetch_bluesky_trends(limit: int = 10) -> list[BskyTrend]:
    data = _bsky("app.bsky.unspecced.getTrends", limit=max(1, min(limit, 25)))
    if not data:
        return []
    seen: dict[str, BskyTrend] = {}
    for t in (data.get("trends") or []):
        name = (t.get("displayName") or t.get("topic") or "").strip()
        if not name:
            continue
        item = BskyTrend(
            topic=name,
            post_count=int(t.get("postCount") or 0),
            status=t.get("status") or "",
            started_at=t.get("startedAt") or "",
            url="https://bsky.app" + t["link"] if t.get("link", "").startswith("/")
                else t.get("link", ""),
        )
        # API 有時會回同名話題兩筆，留貼文數多的那個
        if name not in seen or item.post_count > seen[name].post_count:
            seen[name] = item

    out = sorted(seen.values(), key=lambda t: t.post_count, reverse=True)
    print(f"  · Bluesky 熱門話題：{len(out)} 個")
    return out[:limit]


def _post_url(uri: str, handle: str) -> str:
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    return f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey and handle else ""


def fetch_bluesky_posts(queries: list[str], hours: int = 30, min_likes: int = 10,
                        per_query: int = 25, limit: int = 18,
                        langs: tuple[str, ...] = ("zh", "en", "ja")) -> list[BskyPost]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: set[str] = set()
    posts: list[BskyPost] = []
    skipped = 0

    for query in queries:
        data = _bsky("app.bsky.feed.searchPosts", q=query, sort="top",
                     limit=per_query, since=since)
        for raw in (data or {}).get("posts", []):
            uri = raw.get("uri", "")
            if uri in seen:
                continue
            record = raw.get("record") or {}
            text = (record.get("text") or "").strip()
            likes = int(raw.get("likeCount") or 0)

            # 過濾：讚數門檻、必須命中強訊號、排除洗版與標籤農場
            if likes < min_likes or not text:
                skipped += 1
                continue
            if not _matches_strong(f"{query} {text}") or SPAM.search(text):
                skipped += 1
                continue
            if text.count("#") > 5:
                skipped += 1
                continue
            # 有標語言且不在允許清單內就跳過（沒標語言的保留）
            post_langs = [str(x).split("-")[0] for x in (record.get("langs") or [])]
            if post_langs and not set(post_langs) & set(langs):
                skipped += 1
                continue

            seen.add(uri)
            author = raw.get("author") or {}
            embed = raw.get("embed") or {}
            images = embed.get("images") or []
            external = (embed.get("external") or {}).get("uri", "")
            posts.append(BskyPost(
                text=text[:400],
                handle=author.get("handle", ""),
                display_name=author.get("displayName") or author.get("handle", ""),
                likes=likes,
                reposts=int(raw.get("repostCount") or 0),
                replies=int(raw.get("replyCount") or 0),
                created_at=record.get("createdAt", ""),
                url=_post_url(uri, author.get("handle", "")),
                image=(images[0].get("thumb") or images[0].get("fullsize") or "") if images else "",
                external=external,
                query=query,
            ))

    posts.sort(key=lambda p: (p.likes + p.reposts * 2), reverse=True)
    print(f"  · Bluesky AI 貼文：{len(posts)} 篇符合條件（篩掉 {skipped} 篇雜訊）")
    return posts[:limit]


# ------------------------------------------------------------------ 進入點


def build(cfg: dict, tz: str = "Asia/Taipei") -> SocialDigest:
    now = datetime.now(ZoneInfo(tz))
    hours = int(cfg.get("hours", 30))
    print("抓取社群熱門…")

    digest = SocialDigest(
        date=now.strftime("%Y-%m-%d"),
        generated_at=now.isoformat(),
        timezone=tz,
        window_hours=hours,
        trends=fetch_google_trends(cfg.get("trends_geo", "TW"),
                                   int(cfg.get("trends_max", 10))),
        bsky_trends=fetch_bluesky_trends(int(cfg.get("bluesky_trends_max", 10))),
        bsky_posts=fetch_bluesky_posts(
            list(cfg.get("bluesky_queries", ["OpenAI", "Anthropic", "LLM"])),
            hours=hours,
            min_likes=int(cfg.get("bluesky_min_likes", 10)),
            limit=int(cfg.get("bluesky_posts_max", 18)),
            langs=tuple(cfg.get("bluesky_langs", ["zh", "en", "ja"])),
        ),
        dailyview=fetch_dailyview(int(cfg.get("dailyview_max", 8)))
        if cfg.get("dailyview", True) else [],
        boards=fetch_rank_boards(list(cfg.get("boards", DEFAULT_BOARDS)),
                                 int(cfg.get("board_items", 4))),
    )
    print(f"社群熱門：{len(digest.trends)} 熱搜 / {len(digest.bsky_trends)} 話題 / "
          f"{len(digest.bsky_posts)} 貼文 / {len(digest.dailyview)} 溫度計 / "
          f"{sum(len(b.items) for b in digest.boards)} 排行（{len(digest.boards)} 平台）")
    return digest


# ------------------------------------------- 4) 網路溫度計 dailyview.tw/popular


DV_BASE = "https://dailyview.tw"


def fetch_dailyview(limit: int = 8, timeout: int = 25) -> list[HotArticle]:
    """dailyview 是 Next.js，資料放在 RSC payload 裡，沒有 RSS 也沒有公開 API。

    以「每個 ArticleList_block 為一個單位」解析，避免用兩串 list 對位而錯開。
    """
    try:
        response = requests.get(f"{DV_BASE}/popular", headers={"User-Agent": UA}, timeout=timeout)
        if response.status_code != 200:
            print(f"  ! 網路溫度計: HTTP {response.status_code}")
            return []
    except requests.RequestException as exc:
        print(f"  ! 網路溫度計: {type(exc).__name__}")
        return []

    raw = response.text.replace('\\"', '"').replace("\\n", " ")
    out: list[HotArticle] = []
    for chunk in raw.split("ArticleList_block__")[1:]:
        chunk = chunk[:4000]
        def one(pattern: str) -> str:
            m = re.search(pattern, chunk, re.S)
            return html_lib.unescape(m.group(1)).strip() if m else ""

        path = one(r'"pathname":"(/popular/detail/\d+)"')
        title = one(r'"h2",null,\{"children":"([^"]+)"')
        if not path or not title:
            continue
        out.append(HotArticle(
            title=title,
            url=DV_BASE + path,
            summary=one(r'ArticleList_desc__\w+","children":"([^"]*)"')[:200],
            category=one(r'ArticleList_label__\w+","children":"([^"]+)"'),
            date=one(r'"time",null,\{"children":"([^"]+)"'),
            image=one(r'"src":"(https://[^"]+?\.(?:jpe?g|png|webp)[^"]*)"'),
        ))
        if len(out) >= limit:
            break
    print(f"  · 網路溫度計：{len(out)} 篇熱門話題")
    return out


# ------------------------------------------- 5) Social Lab 各平台排行榜


def _short_period(match: re.Match | None) -> str:
    """把「觀測時間：2026.07.20-2026.07.26」壓成「07/20–07/26」，避免卡片內折行。"""
    if not match:
        return ""
    def fmt(raw: str) -> str:
        parts = raw.strip(".").split(".")
        return "/".join(parts[1:]) if len(parts) == 3 else raw
    return f"{fmt(match.group(1))}–{fmt(match.group(2))}"


def fetch_rank_board(platform: str, url: str, limit: int = 4,
                     timeout: int = 25) -> RankBoard:
    """Social Lab（OpView）的平台排行榜頁：每週一篇彙整文章。

    榜上的數字是圖表圖片，抓不到；但週報標題本身就是熱門事件摘要，這才是有用的部分。
    """
    board = RankBoard(platform=platform, url=url)
    try:
        response = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
        if response.status_code != 200:
            print(f"  ! Social Lab {platform}: HTTP {response.status_code}")
            return board
    except requests.RequestException as exc:
        print(f"  ! Social Lab {platform}: {type(exc).__name__}")
        return board

    # WordPress 的 Visual Portfolio：每個 <article class="vp-portfolio__item-wrap"> 一則
    for chunk in response.text.split('class="vp-portfolio__item-wrap')[1:]:
        chunk = chunk[:6000]
        # 各頁的 <a> 寫法不一致：href 前有換行與 tab，Dcard 還多了 target/rel 屬性
        link = re.search(
            r'vp-portfolio__item-meta-title"[^>]*>\s*<a\s[^>]*?href="([^"]+)"[^>]*>\s*(.+?)\s*</a>',
            chunk, re.S)
        if not link:
            continue
        period_raw = re.search(r"觀測時間：\s*([\d.]+)\s*-\s*([\d.]+)", chunk)
        image = re.search(r'<img[^>]+src="(https://[^"]+?\.(?:jpe?g|png|webp))"', chunk)
        board.items.append(RankItem(
            title=html_lib.unescape(link.group(2)).strip(),
            url=link.group(1),
            period=_short_period(period_raw),
            image=(image.group(1) if image else ""),
        ))
        if len(board.items) >= limit * 4:      # 多抓一些再排序，避免漏掉最新的
            break
    # 依觀測期間新到舊排序（各頁的 DOM 順序不一定一致），再取前 limit 則
    board.items.sort(key=lambda i: i.period, reverse=True)
    board.items = board.items[:limit]
    print(f"  · Social Lab {platform}：{len(board.items)} 則")
    return board


DEFAULT_BOARDS = [
    {"name": "PTT", "url": "https://www.social-lab.cc/ptt_charts/"},
    {"name": "Facebook", "url": "https://www.social-lab.cc/facebook-charts/"},
    {"name": "Dcard", "url": "https://www.social-lab.cc/dcard_charts/"},
    {"name": "Instagram", "url": "https://www.social-lab.cc/instagram%e6%8e%92%e8%a1%8c%e6%a6%9c/"},
    {"name": "Mobile01", "url": "https://www.social-lab.cc/mobile01-charts/"},
    {"name": "YouTube", "url": "https://www.social-lab.cc/hot-youtube/"},
    {"name": "新聞", "url": "https://www.social-lab.cc/news_charts/"},
]


def fetch_rank_boards(boards: list[dict], per_board: int = 4) -> list[RankBoard]:
    if not boards:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(boards))) as pool:
        return [b for b in pool.map(
            lambda cfg: fetch_rank_board(cfg["name"], cfg["url"], per_board), boards)
            if b.items]
