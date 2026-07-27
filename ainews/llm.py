"""選用步驟：請 LLM 為每個主題寫中文標題／整合摘要／分類／重要度。

支援兩種後端：

* `provider = "openai"`（預設）—— 任何 OpenAI-compatible 的 `/v1/chat/completions`
  端點：自架 vLLM / Ollama / LiteLLM、公司內部 gateway、OpenRouter、OpenAI 本家…
  只要給 base_url、model、api_key 三樣。
* `provider = "anthropic"` —— 直接走 Anthropic Messages API。

沒有設定或呼叫失敗時會安靜跳過，分群結果照樣能出 HTML；
在 Claude Cowork 裡也可以完全不用這一段（見 README 的 Cowork 章節）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .cluster import Topic

CATEGORIES = [
    "模型與研究", "產品與應用", "企業與資金", "晶片與基礎設施",
    "政策與法規", "安全與倫理", "開源社群", "其他",
]

SYSTEM = """你是科技新聞編輯，負責產出每日 AI 新聞摘要（繁體中文，台灣用語）。

輸入是已經初步分群的新聞主題，每個主題含一到多篇來自不同媒體的報導。針對每個主題：

1. title：一句話標題，20-40 字，具體寫出「誰做了什麼」，不要用「震撼」「重磅」這類聳動詞。
2. summary：2-3 句整合摘要。綜合該主題下所有來源的資訊，寫出事件本身、關鍵數字或版本名稱、以及為什麼值得注意。不要逐條複述標題，不要寫「根據報導」。
3. category：從以下分類選一個最貼切的：模型與研究／產品與應用／企業與資金／晶片與基礎設施／政策與法規／安全與倫理／開源社群／其他
4. importance：1-5 的重要性（5＝產業級重大事件，3＝值得一讀，1＝邊緣消息）。多家媒體同時報導通常代表較重要。
5. duplicate_of：如果這個主題其實和同一批輸入中「另一個 key」講的是同一件事，填那個 key（填較早出現的那個）；否則填空字串。

只依據輸入內容作答，不要補充輸入裡沒有的事實或數字。
輸出必須是 JSON 物件，格式為 {"topics": [{"key": ..., "title": ..., "summary": ..., "category": ..., "importance": ..., "duplicate_of": ...}]}，
輸入的每一個 key 都要有對應的一筆結果，不要輸出 JSON 以外的任何文字。"""

_TOPIC_PROPS = {
    "key": {"type": "string"},
    "title": {"type": "string"},
    "summary": {"type": "string"},
    "category": {"type": "string", "enum": CATEGORIES},
    "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
    "duplicate_of": {"type": "string"},
}
SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _TOPIC_PROPS,
                "required": list(_TOPIC_PROPS),
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- 設定


def load_dotenv(path: Path) -> None:
    """讀取 .env（KEY=VALUE），已存在的環境變數優先，不覆寫。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class LLMConfig:
    provider: str = "openai"        # openai | anthropic
    base_url: str = ""              # 例：https://your-gateway/v1
    model: str = ""                 # 例：gpt-4o-mini、qwen2.5-72b-instruct
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 8000
    timeout: int = 180
    batch_size: int = 12
    json_mode: str = "auto"         # auto | json_schema | json_object | prompt
    effort: str = "high"            # 只有 provider = anthropic 會用到
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, section: dict) -> LLMConfig:
        """config.toml 的 [llm] 為底，環境變數優先（API key 別寫進檔案）。"""
        cfg = cls(
            provider=os.environ.get("LLM_PROVIDER") or section.get("provider", "openai"),
            base_url=os.environ.get("LLM_BASE_URL") or section.get("base_url", ""),
            model=os.environ.get("LLM_MODEL") or section.get("model", ""),
            api_key=os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", ""),
            temperature=float(section.get("temperature", 0.2)),
            max_tokens=int(section.get("max_tokens", 8000)),
            timeout=int(section.get("timeout", 180)),
            batch_size=int(section.get("batch_size", 12)),
            json_mode=os.environ.get("LLM_JSON_MODE") or section.get("json_mode", "auto"),
            effort=section.get("effort", "high"),
            extra_headers=dict(section.get("extra_headers", {})),
        )
        if cfg.provider == "anthropic" and not cfg.api_key:
            cfg.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        return cfg

    def describe(self) -> str:
        if self.provider == "anthropic":
            return f"anthropic / {self.model or 'claude-opus-4-8'}"
        return f"{self.model or '(未設定 model)'} @ {self.base_url or '(未設定 base_url)'}"

    def missing(self) -> list[str]:
        """回傳還缺哪些設定；空清單代表可以用了。"""
        if self.provider == "anthropic":
            return [] if _anthropic_ready(self) else ["ANTHROPIC_API_KEY"]
        gaps = []
        if not self.base_url:
            gaps.append("LLM_BASE_URL")
        if not self.model:
            gaps.append("LLM_MODEL")
        if not self.api_key:
            gaps.append("LLM_API_KEY")
        return gaps


def available(cfg: LLMConfig) -> bool:
    return not cfg.missing()


def _anthropic_ready(cfg: LLMConfig) -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    if cfg.api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.expanduser("~/.config/anthropic")
    return os.path.isdir(os.path.join(config_dir, "credentials"))


# --------------------------------------------------------------- JSON 解析（容錯）


def _strip_wrappers(text: str) -> str:
    text = re.sub(r"<(think|thinking|reasoning)>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"```(?:json)?\s*(.*?)```", r"\1", text, flags=re.S)
    return text.strip()


def _balanced_objects(text: str):
    """掃出所有大括號配對的片段（跳過字串內的括號），從最長的開始試。"""
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in starts:
        depth, in_string, escaped = 0, False, False
        for pos in range(start, len(text)):
            ch = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:pos + 1]
                    break


def _extract_json(text: str) -> dict | None:
    """從模型輸出裡挖出 {"topics": [...]}，容忍前後贅字、思考標籤、程式碼圍籬。"""
    cleaned = _strip_wrappers(text)
    for candidate in (cleaned, *_balanced_objects(cleaned)):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("topics"), list):
            return data
    return None


# ------------------------------------------------------- OpenAI-compatible 後端


def chat_endpoint(base_url: str) -> str:
    """把使用者給的 URL 補成 chat/completions 端點。

    https://host                → https://host/v1/chat/completions
    https://host/v1             → https://host/v1/chat/completions
    https://host/v1/chat/completions → 原樣使用
    """
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if re.search(r"/v\d+[a-z-]*$", url) or url.endswith("/openai"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def _response_format(mode: str) -> dict | None:
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {"name": "daily_topics", "strict": True, "schema": SCHEMA},
        }
    if mode == "json_object":
        return {"type": "json_object"}
    return None


def _openai_chat(cfg: LLMConfig, user: str, mode: str) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
        **cfg.extra_headers,
    }
    body: dict = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    fmt = _response_format(mode)
    if fmt:
        body["response_format"] = fmt

    response = requests.post(
        chat_endpoint(cfg.base_url), headers=headers, json=body, timeout=cfg.timeout
    )
    if response.status_code != 200:
        snippet = response.text[:300].replace("\n", " ")
        raise _HTTPError(response.status_code, snippet)

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"回應不是 JSON：{response.text[:200]}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"回應沒有 choices：{json.dumps(payload, ensure_ascii=False)[:200]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):   # 少數 gateway 會回 content blocks
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise RuntimeError("回應的 message.content 是空的（可能被 max_tokens 截斷）")
    return content


class _HTTPError(RuntimeError):
    def __init__(self, status: int, snippet: str) -> None:
        super().__init__(f"HTTP {status}: {snippet}")
        self.status = status
        self.snippet = snippet


def _mode_unsupported(exc: _HTTPError) -> bool:
    """判斷這次失敗是不是因為 gateway 不吃 response_format。"""
    if exc.status not in (400, 404, 422, 500, 501):
        return False
    low = exc.snippet.lower()
    return any(k in low for k in ("response_format", "json_schema", "json mode", "unsupported", "unrecognized", "invalid_request"))


# ------------------------------------------------------------------ 主要進入點


def _payload(topics: list[Topic]) -> str:
    items = [
        {
            "key": topic.key,
            "sources": topic.sources,
            "articles": [
                {"source": a.source, "title": a.title, "summary": a.summary[:400]}
                for a in topic.articles[:6]
            ],
        }
        for topic in topics
    ]
    return json.dumps(items, ensure_ascii=False, indent=1)


def _anthropic_batch(cfg: LLMConfig, batch: list[Topic]) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.api_key or None)
    request = dict(
        model=cfg.model or "claude-opus-4-8",
        max_tokens=max(cfg.max_tokens, 8000),
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={
            "effort": cfg.effort,
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        messages=[{"role": "user", "content": _user_prompt(batch)}],
    )
    try:
        with client.messages.stream(**request) as stream:
            message = stream.get_final_message()
    except TypeError:   # 較舊的 SDK 不認得 output_config
        output_config = request.pop("output_config")
        request["extra_body"] = {"output_config": output_config}
        with client.messages.stream(**request) as stream:
            message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError("模型拒絕處理這批內容（stop_reason=refusal）")
    text = "".join(b.text for b in message.content if b.type == "text")
    data = _extract_json(text)
    if not data:
        raise RuntimeError("無法解析回傳的 JSON")
    return data["topics"]


def _user_prompt(batch: list[Topic]) -> str:
    return f"以下是今日 AI 新聞主題（JSON）。請為每一個 key 都輸出一筆結果。\n\n{_payload(batch)}"


class _OpenAIBackend:
    """記住哪一種 JSON 模式在這個 gateway 上行得通，後續批次直接沿用。"""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        if cfg.json_mode == "auto":
            self.modes = ["json_schema", "json_object", "prompt"]
        else:
            self.modes = [cfg.json_mode]

    def batch(self, batch: list[Topic]) -> list[dict]:
        user = _user_prompt(batch)
        last: Exception | None = None
        while self.modes:
            mode = self.modes[0]
            try:
                text = _openai_chat(self.cfg, user, mode)
            except _HTTPError as exc:
                last = exc
                if len(self.modes) > 1 and _mode_unsupported(exc):
                    print(f"  · 端點不支援 {mode}，改用 {self.modes[1]}（{exc.snippet[:80]}）")
                    self.modes.pop(0)
                    continue
                raise
            data = _extract_json(text)
            if data:
                return data["topics"]
            last = RuntimeError(f"無法解析回傳的 JSON：{text[:160]}")
            if len(self.modes) > 1 and mode != "prompt":
                print(f"  · {mode} 的輸出解析不了，改用 {self.modes[1]}")
                self.modes.pop(0)
                continue
            raise last
        raise last or RuntimeError("沒有可用的 JSON 模式")


def selftest(cfg: LLMConfig) -> bool:
    """打一次最小請求，確認 URL／model／key 都對。"""
    print(f"測試 LLM 連線：{cfg.describe()}")
    gaps = cfg.missing()
    if gaps:
        print(f"✗ 缺少設定：{', '.join(gaps)}")
        return False

    from .fetch import Article

    probe = Topic(
        key="t0000",
        title="輝達擬提供 2,500 億美元擔保，助 OpenAI 建資料中心",
        articles=[
            Article(
                title="輝達擬提供 2,500 億美元擔保，助 OpenAI 建資料中心",
                url="https://example.com/a", source="測試來源 A", lang="zh",
                published="2026-01-01T00:00:00+00:00",
                summary="輝達計劃為 OpenAI 的資料中心建設提供高達 2,500 億美元的債務擔保。",
            ),
            Article(
                title="Nvidia in talks to backstop $250B of OpenAI data center debt",
                url="https://example.com/b", source="測試來源 B", lang="en",
                published="2026-01-01T00:00:00+00:00",
                summary="Nvidia would guarantee debt raised for OpenAI's largest data center project.",
            ),
        ],
    )
    try:
        if cfg.provider == "anthropic":
            result = _anthropic_batch(cfg, [probe])
        else:
            result = _OpenAIBackend(cfg).batch([probe])
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 失敗：{exc}")
        if cfg.provider != "anthropic":
            print(f"  端點：{chat_endpoint(cfg.base_url)}")
        return False

    print(f"✓ 成功，回傳 {len(result)} 筆：{json.dumps(result[:1], ensure_ascii=False)[:220]}")
    return True


def enrich(topics: list[Topic], cfg: LLMConfig) -> list[Topic]:
    """就地補上中文標題與摘要，並合併模型判定為重複的主題。"""
    if not topics:
        return topics

    backend = _OpenAIBackend(cfg) if cfg.provider != "anthropic" else None
    by_key = {t.key: t for t in topics}
    merges: dict[str, str] = {}

    failures = 0
    for start in range(0, len(topics), cfg.batch_size):
        batch = topics[start:start + cfg.batch_size]
        label = f"{start + 1}-{start + len(batch)}/{len(topics)}"
        try:
            results = backend.batch(batch) if backend else _anthropic_batch(cfg, batch)
        except Exception as exc:  # noqa: BLE001 - 摘要失敗就保留演算法版本
            failures += 1
            print(f"  ! 摘要失敗（主題 {label}）：{exc}")
            if failures >= 2:
                # 連兩批都失敗通常是設定或服務的問題，不值得把剩下的批次也慢慢試一遍
                print("  ! 連續失敗，放棄 LLM 摘要，其餘主題沿用原文標題")
                break
            continue
        failures = 0

        wrote = 0
        for item in results:
            topic = by_key.get(str(item.get("key", "")))
            if topic is None:
                continue
            topic.title = (item.get("title") or topic.title).strip()
            topic.summary = (item.get("summary") or topic.summary).strip()
            category = str(item.get("category") or "").strip()
            topic.category = category if category in CATEGORIES else topic.category
            try:
                topic.importance = min(5, max(1, int(item.get("importance") or 3)))
            except (TypeError, ValueError):
                topic.importance = 3
            topic.llm_enriched = True
            wrote += 1
            target = str(item.get("duplicate_of") or "").strip()
            if target and target in by_key and target != topic.key:
                merges[topic.key] = target
        print(f"  · 已整理主題 {label}（{wrote} 筆）")

    return _apply_merges(topics, merges)


def _apply_merges(topics: list[Topic], merges: dict[str, str]) -> list[Topic]:
    if not merges:
        return topics

    def root(key: str, depth: int = 0) -> str:
        # depth 上限避免 A→B→A 的循環
        return root(merges[key], depth + 1) if key in merges and depth < 8 else key

    by_key = {t.key: t for t in topics}
    absorbed: set[str] = set()
    for key in list(merges):
        target = root(key)
        if target == key or target not in by_key or key not in by_key:
            continue
        host, guest = by_key[target], by_key[key]
        seen = {a.url for a in host.articles}
        host.articles.extend(a for a in guest.articles if a.url not in seen)
        host.articles.sort(key=lambda a: a.published, reverse=True)
        host.importance = max(host.importance, guest.importance)
        absorbed.add(key)

    if absorbed:
        print(f"  · 合併了 {len(absorbed)} 個重複主題")
    return [t for t in topics if t.key not in absorbed]
