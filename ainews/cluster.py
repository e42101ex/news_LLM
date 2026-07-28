"""把講同一件事的新聞分到同一群 —— 純演算法，不需要 API key。

做法：TF-IDF 向量 + cosine 相似度 + 單一連結分群（union-find）。
中文用「字元 bigram」當 token，英文用詞幹，所以中英夾雜的標題也能對上
（例如「OpenAI 發表新模型」與「OpenAI releases new model」會共享 openai 這個 token）。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .fetch import Article

CJK_RUN = re.compile(r"[一-鿿぀-ヿ가-힯]+")
LATIN = re.compile(r"[a-z][a-z0-9\-+.]{1,}")
NUMBER = re.compile(r"\d[\d.]{2,}")

# 簡體轉繁體：讓 IT之家（簡）與科技新報（繁）的同一則新聞可以對上。
# 沒裝 opencc 也能跑，只是跨簡繁的分群會差一些。
try:  # pragma: no cover - 取決於環境有沒有安裝
    from opencc import OpenCC

    _CC: object | None = OpenCC("s2t")
except Exception:  # noqa: BLE001
    _CC = None

# 實體同義詞：把各語言寫法收斂成同一個 token，補足「中英文各報一次」的情況。
# 左邊是標準 token，右邊是各家寫法（簡體會先被轉成繁體，所以只需寫繁體與英文）。
ENTITY_ALIASES: dict[str, list[str]] = {
    "nvidia": ["nvidia", "輝達", "英偉達", "黃仁勳", "jensen huang", "geforce"],
    "openai": ["openai", "奧特曼", "阿爾特曼", "sam altman", "chatgpt", "sora"],
    "anthropic": ["anthropic", "claude"],
    "google": ["google", "谷歌", "alphabet", "gemini", "deepmind", "pichai"],
    "microsoft": ["microsoft", "微軟", "copilot", "azure"],
    "meta": ["meta", "臉書", "facebook", "instagram", "llama"],
    "apple": ["apple", "蘋果", "iphone", "siri"],
    "amazon": ["amazon", "亞馬遜", "aws", "bedrock"],
    "tsmc": ["tsmc", "台積電", "台積", "台机电"],
    "samsung": ["samsung", "三星"],
    "sk-hynix": ["sk hynix", "sk海力士", "海力士"],
    "amd": ["amd", "超微"],
    "intel": ["intel", "英特爾"],
    "alibaba": ["alibaba", "阿里巴巴", "阿里雲", "通義", "qwen"],
    "bytedance": ["bytedance", "字節跳動", "抖音", "tiktok", "豆包"],
    "tencent": ["tencent", "騰訊", "混元"],
    "baidu": ["baidu", "百度", "文心"],
    "huawei": ["huawei", "華為", "昇騰", "鴻蒙"],
    "xiaomi": ["xiaomi", "小米", "澎湃"],
    "deepseek": ["deepseek", "深度求索", "梁文鋒"],
    "mistral": ["mistral"],
    "xai": ["xai", "grok", "馬斯克", "musk"],
    "datacenter": ["data center", "datacenter", "資料中心", "數據中心", "機房"],
    "chip": ["chip", "semiconductor", "晶片", "芯片", "半導體", "製程", "奈米", "納米"],
    "hbm": ["hbm", "高頻寬記憶體", "高帶寬記憶體", "dram", "記憶體"],
    "compute": ["算力", "compute cluster", "gpu cluster", "運算叢集"],
    "layoff": ["layoff", "裁員", "失業", "job cut", "職缺"],
    "funding": ["funding", "raise", "valuation", "融資", "估值", "投資", "億美元", "億元"],
    "regulation": ["regulation", "regulator", "監管", "法規", "法案", "禁令", "eu ai act"],
    "opensource": ["open source", "open-source", "開源", "hugging face", "huggingface"],
    "agent": ["ai agent", "agentic", "智慧體", "智能體", "代理"],
    "robot": ["robot", "robotics", "機器人", "具身智慧", "具身智能"],
    "safety": ["ai safety", "alignment", "對齊", "資安", "網路安全", "cybersecurity"],
}

_ALIAS_INDEX: list[tuple[str, str]] = sorted(
    ((variant.lower(), canonical) for canonical, variants in ENTITY_ALIASES.items()
     for variant in variants),
    key=lambda pair: len(pair[0]),
    reverse=True,
)

STOP = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "was", "are",
    "will", "its", "into", "than", "then", "but", "not", "you", "your", "our", "their",
    "new", "now", "how", "why", "what", "who", "when", "more", "most", "can", "could",
    "would", "about", "after", "before", "over", "under", "out", "off", "all", "one",
    "two", "says", "said", "say", "just", "also", "been", "being", "get", "gets",
    "報導", "表示", "指出", "宣布", "宣佈", "發表", "推出", "公司", "今天", "今日",
    "可以", "已經", "我們", "他們", "一個", "這個", "的", "了", "是", "在", "和",
}


@dataclass
class Topic:
    key: str
    title: str                              # 代表標題（LLM 未介入時就是最具代表性的原標題）
    summary: str = ""
    category: str = "其他"
    importance: int = 3
    articles: list[Article] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    image: str = ""            # 代表縮圖：docs/ 底下的相對路徑，或遠端 URL
    llm_enriched: bool = False

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for art in self.articles:
            if art.source not in seen:
                seen.append(art.source)
        return seen

    @property
    def latest(self) -> str:
        return max((a.published for a in self.articles), default="")

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "category": self.category,
            "importance": self.importance,
            "keywords": self.keywords,
            "image": self.image,
            "llm_enriched": self.llm_enriched,
            "sources": self.sources,
            "articles": [a.to_dict() for a in self.articles],
        }


def normalize(text: str) -> str:
    """統一大小寫與簡繁，讓不同媒體的寫法可以比較。"""
    if _CC is not None and CJK_RUN.search(text):
        try:
            text = _CC.convert(text)
        except Exception:  # noqa: BLE001
            pass
    # 2,500 → 2500，讓「2,500 億美元」和「2500 亿美元」共享同一個 token
    text = re.sub(r"(?<=\d)[,，](?=\d)", "", text)
    return text.lower()


def _alias_tokens(normalized: str) -> list[str]:
    """抽出實體同義 token（加權 3 倍，因為公司／主題名稱是最強的分群訊號）。"""
    found: list[str] = []
    for variant, canonical in _ALIAS_INDEX:
        if variant in normalized:
            found.append(f"@{canonical}")
    return [tok for tok in dict.fromkeys(found) for _ in range(3)]


def tokenize(text: str) -> list[str]:
    low = normalize(text)
    tokens = _alias_tokens(low)
    tokens += [t for t in LATIN.findall(low) if t not in STOP and len(t) > 2]
    tokens += [n.rstrip(".") for n in NUMBER.findall(low)]
    for run in CJK_RUN.findall(low):
        if len(run) == 1:
            tokens.append(run)
            continue
        # 字元 bigram：中文沒有空白，bigram 是最穩的免斷詞做法
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return [t for t in tokens if t not in STOP]


def _vectors(docs: list[list[str]]) -> tuple[list[dict[str, float]], Counter[str]]:
    n = len(docs)
    df: Counter[str] = Counter()
    for toks in docs:
        df.update(set(toks))

    vectors: list[dict[str, float]] = []
    for toks in docs:
        tf = Counter(toks)
        vec = {
            tok: (1 + math.log(count)) * math.log((n + 1) / (df[tok] + 1))
            for tok, count in tf.items()
        }
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({k: v / norm for k, v in vec.items()})
    return vectors, df


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(tok, 0.0) for tok, weight in a.items())


def _add(centroid_sum: dict[str, float], vec: dict[str, float]) -> None:
    for tok, weight in vec.items():
        centroid_sum[tok] = centroid_sum.get(tok, 0.0) + weight


def _normalized(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}


def cluster(articles: list[Article], threshold: float = 0.26) -> list[Topic]:
    """把講同一件事的文章分到同一群。

    用「質心式（leader）分群」而非 single-linkage：一篇文章要加入某群，必須和
    該群『整體的質心』夠像。single-linkage 只要和群裡任一篇像就會合併，文章量
    一大就會串成一團（例如把「Claude 對話外洩」和「Verizon 暗光纖交易」併在一起）。
    """
    if not articles:
        return []

    # 標題權重加倍（標題比摘要更能代表主題）
    docs = [tokenize(f"{a.title} {a.title} {a.summary}") for a in articles]
    vectors, df = _vectors(docs)
    token_sets = [set(d) for d in docs]
    idf = {tok: math.log((len(docs) + 1) / (count + 1)) for tok, count in df.items()}

    def shares_signature(left: int, right: int) -> bool:
        """兩篇是否共享『同一個主角 + 具辨識度的細節』。

        例如 @nvidia + vera + eda，或 @nvidia + 2500（億美元）。這種組合幾乎
        一定是同一則新聞，即使一篇是英文、一篇是簡體中文，cosine 也不會高。
        """
        shared = token_sets[left] & token_sets[right]
        if not any(tok.startswith("@") for tok in shared):
            return False

        distinctive = 0
        for tok in shared:
            if tok.startswith("@") or idf.get(tok, 0.0) < 1.5 or not tok.isascii():
                continue
            if tok[0].isdigit() and len(tok) >= 3:
                return True          # 對得上的大數字（金額、型號）本身就足夠
            if len(tok) >= 3:
                distinctive += 1
        return distinctive >= 2

    # 新的排在前面當「群首」，比較容易長出以最新報導為代表的主題
    order = sorted(range(len(articles)), key=lambda i: articles[i].published, reverse=True)

    groups: list[dict] = []      # {members: [idx], sum: 質心累加, centroid: 正規化質心}
    for idx in order:
        best, best_sim = None, 0.0
        for group in groups:
            sim = _cosine(vectors[idx], group["centroid"])
            if sim > best_sim:
                best, best_sim = group, sim

        join = False
        if best is not None:
            if best_sim >= threshold:
                join = True
            elif best_sim >= threshold * 0.35:
                # 相似度不足時的例外：跨語言／跨簡繁的同一則新聞。要求和群裡
                # 「每一篇」都共享主角＋辨識細節，避免又變成鏈式誤併。
                join = all(shares_signature(idx, member) for member in best["members"])

        if join:
            best["members"].append(idx)
            _add(best["sum"], vectors[idx])
            best["centroid"] = _normalized(best["sum"])
        else:
            centroid_sum = dict(vectors[idx])
            groups.append({"members": [idx], "sum": centroid_sum,
                           "centroid": _normalized(centroid_sum)})

    # 貪婪分配會受處理順序影響：先形成的群可能把後來更適合別群的文章吸走。
    # 做兩輪「重新分配到最近質心」（k-means 式）修正，只在明確更好時才搬動。
    for _ in range(2):
        moved = 0
        for group in groups:
            for idx in list(group["members"]):
                if len(group["members"]) == 1:
                    continue
                current = _cosine(vectors[idx], group["centroid"])
                target, target_sim = None, current
                for other in groups:
                    if other is group:
                        continue
                    sim = _cosine(vectors[idx], other["centroid"])
                    if sim > target_sim:
                        target, target_sim = other, sim
                # 只有「新群明顯更像」且超過門檻才搬，避免破壞跨語言的例外合併
                if target is not None and target_sim >= threshold and target_sim > current + 0.05:
                    group["members"].remove(idx)
                    target["members"].append(idx)
                    for holder in (group, target):
                        holder["sum"] = {}
                        for member in holder["members"]:
                            _add(holder["sum"], vectors[member])
                        holder["centroid"] = _normalized(holder["sum"])
                    moved += 1
        if not moved:
            break
    groups = [g for g in groups if g["members"]]

    topics: list[Topic] = []
    for n, group in enumerate(groups):
        idxs = group["members"]
        members = sorted((articles[i] for i in idxs), key=lambda a: a.published, reverse=True)

        # 代表標題：和質心最接近的那篇（最能代表整群）
        lead = max(idxs, key=lambda i: _cosine(vectors[i], group["centroid"]))
        rep = articles[lead]

        merged: Counter[str] = Counter()
        for i in idxs:
            merged.update(vectors[i])
        keywords: list[str] = []
        for tok, _ in merged.most_common(30):
            label = tok[1:] if tok.startswith("@") else tok
            if len(label) > 2 and not CJK_RUN.fullmatch(label) and label not in keywords:
                keywords.append(label)

        topics.append(Topic(
            key=f"t{n:04d}",
            title=rep.title,
            summary=rep.summary[:280],
            articles=members,
            keywords=keywords[:6],
        ))

    # 排序：來源數多的（多家報導＝重要）優先，其次時間新
    topics.sort(key=lambda t: (len(t.sources), len(t.articles), t.latest), reverse=True)
    return topics
