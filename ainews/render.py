"""把主題列表渲染成 HTML（今日頁 + 日期存檔 + 存檔索引）。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .cluster import Topic
from .fetch import Article

CATEGORY_ORDER = [
    "模型與研究", "產品與應用", "企業與資金", "晶片與基礎設施",
    "政策與法規", "安全與倫理", "開源社群", "其他",
]
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["localtime"] = _localtime
    env.filters["domain"] = _domain
    return env


def _localtime(iso: str, tz: str = "Asia/Taipei") -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz)).strftime("%m/%d %H:%M")


def _domain(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "")
    return match.group(1).replace("www.", "") if match else ""


def topics_to_json(topics: list[Topic], tz: str, hours: int) -> dict:
    now = datetime.now(ZoneInfo(tz))
    return {
        "generated_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "window_hours": hours,
        "timezone": tz,
        "topic_count": len(topics),
        "article_count": sum(len(t.articles) for t in topics),
        "topics": [t.to_dict() for t in topics],
    }


def json_to_topics(data: dict) -> list[Topic]:
    """反序列化 —— 讓 Claude（或人）改完 JSON 後可以只重跑渲染。"""
    topics: list[Topic] = []
    for raw in data.get("topics", []):
        topics.append(Topic(
            key=raw["key"],
            title=raw["title"],
            summary=raw.get("summary", ""),
            category=raw.get("category", "其他"),
            importance=int(raw.get("importance", 3)),
            keywords=raw.get("keywords", []),
            image=raw.get("image", ""),
            llm_enriched=bool(raw.get("llm_enriched")),
            articles=[Article(**a) for a in raw.get("articles", [])],
        ))
    return topics


def _grouped(topics: list[Topic]) -> list[tuple[str, list[Topic]]]:
    buckets: dict[str, list[Topic]] = {}
    for topic in topics:
        buckets.setdefault(topic.category or "其他", []).append(topic)
    for items in buckets.values():
        items.sort(key=lambda t: (t.importance, len(t.sources), t.latest), reverse=True)
    known = [(c, buckets.pop(c)) for c in CATEGORY_ORDER if c in buckets]
    return known + sorted(buckets.items())


def render(topics: list[Topic], out_dir: Path, *, site_title: str, tz: str,
           hours: int, archive: bool = True) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "archive").mkdir(exist_ok=True)

    meta = topics_to_json(topics, tz, hours)
    template = _env().get_template("index.html.j2")
    page = template.render(
        site_title=site_title,
        meta=meta,
        groups=_grouped(topics),
        sources=sorted({a.source for t in topics for a in t.articles}),
        tz=tz,
        is_archive=False,
        img_prefix="",
    )

    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")

    if archive:
        snapshot = out_dir / "archive" / f"{meta['date']}.html"
        snapshot.write_text(
            template.render(
                site_title=site_title, meta=meta, groups=_grouped(topics),
                sources=sorted({a.source for t in topics for a in t.articles}),
                tz=tz, is_archive=True, img_prefix="../",
            ),
            encoding="utf-8",
        )
        _render_archive_index(out_dir, site_title)

    (out_dir / "data.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out_dir / ".nojekyll").touch()
    return index


def _render_archive_index(out_dir: Path, site_title: str) -> None:
    days = sorted(
        (p.stem for p in (out_dir / "archive").glob("*.html") if p.stem != "index"),
        reverse=True,
    )
    html = _env().get_template("archive.html.j2").render(site_title=site_title, days=days)
    (out_dir / "archive" / "index.html").write_text(html, encoding="utf-8")
