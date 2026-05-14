"""Claude API service for /me personal organization (Phase 5).

Two capabilities:
  - extract_from_daily_log(content, today): structured extract of tasks,
    future actions, meeting decisions, and tags from a free-form daily log.
  - smart_search(query, logs): answer a natural-language question across
    recent daily logs and return supporting source dates.

Both are optional: if ANTHROPIC_API_KEY is missing or the `anthropic`
package is not installed, is_enabled() returns False and the caller
should respond with 503.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

log = logging.getLogger("guild.ai")

try:
    from anthropic import Anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    Anthropic = None  # type: ignore[assignment,misc]
    _ANTHROPIC_AVAILABLE = False


CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Korean is roughly 1 char per token in Claude tokenizers. 80,000 chars keeps
# a single call well under the model's context window and caps cost per call.
MAX_INPUT_CHARS = 80_000

EXTRACT_KIND = "daily_log_extract"
SEARCH_KIND = "search"


def is_enabled() -> bool:
    return _ANTHROPIC_AVAILABLE and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _client() -> Any:
    if not is_enabled():
        raise RuntimeError("AI service is not configured (ANTHROPIC_API_KEY missing)")
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _log_usage(label: str, message: Any) -> None:
    try:
        usage = getattr(message, "usage", None)
        if usage:
            log.info(
                "ai.%s input=%s output=%s model=%s",
                label, getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"), CLAUDE_MODEL,
            )
    except Exception:
        pass


def _collect_text(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip()


# ── 1) Daily log extraction ───────────────────────────────────────

_EXTRACT_SYSTEM = (
    "너는 한국어 일기/업무 노트에서 의미 있는 항목을 추출하는 비서다.\n"
    "사용자가 자유롭게 쓴 텍스트를 읽고 다음 4가지로 분류한다:\n\n"
    "1. tasks: 즉시 또는 단기(1-7일) 안에 해야 할 액션.\n"
    '   예: "내일 김부장에게 회신", "이번 주까지 보고서 작성".\n'
    "2. future: 더 먼 미래(1주 이후) 에 할 일.\n"
    '   예: "다음 달 출장 일정 잡기", "Q3 OKR 검토".\n'
    "3. decisions: 회의나 논의에서 나온 결정사항/요지.\n"
    '   예: "프로젝트 X 일정 2주 연기 결정".\n'
    "4. tags: 텍스트에 나타난 핵심 키워드(프로젝트명, 인물명, 주제 등). 5개 이내.\n\n"
    "반드시 JSON 으로만 응답한다. 코드블록도 사용하지 않는다.\n"
    "형식:\n"
    "{\n"
    '  "tasks":     [{"title":"...","due_hint":"내일|이번주|YYYY-MM-DD|null"}],\n'
    '  "future":    [{"title":"...","due_hint":"다음주|다음달|YYYY-MM-DD|null"}],\n'
    '  "decisions": [{"summary":"..."}],\n'
    '  "tags":      ["...","..."]\n'
    "}\n\n"
    "원문에서 명확하게 액션이나 결정이 보이지 않으면 해당 배열은 빈 배열로 둔다. 추측하지 않는다.\n"
    "title/summary 는 원문 표현을 유지하되 30자 이내로 정리한다.\n"
    "오늘 날짜는 컨텍스트에 주어진다."
)


def extract_from_daily_log(content: str, today: str) -> dict:
    """Call Claude to extract structured items from a free-form daily log.

    Returns {tasks, future, decisions, tags}. Caller handles caching.
    """
    if not content.strip():
        return _empty_extract()

    if len(content) > MAX_INPUT_CHARS:
        content = content[-MAX_INPUT_CHARS:]
        log.warning("ai.extract truncated input to %d chars", MAX_INPUT_CHARS)

    user_prompt = (
        f"오늘은 {today} 입니다.\n\n"
        "다음은 사용자의 하루 로그입니다:\n\n---\n"
        f"{content}\n---\n\n"
        "위 형식의 JSON 으로만 응답해주세요."
    )

    msg = _client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    _log_usage("extract", msg)
    return _parse_extract_json(_collect_text(msg))


def _empty_extract() -> dict:
    return {"tasks": [], "future": [], "decisions": [], "tags": []}


def _strip_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        # remove leading ```json or ``` and trailing ```
        s = s.lstrip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_extract_json(raw: str) -> dict:
    s = _strip_fence(raw)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        log.warning("ai.extract JSON parse failed: %r", raw[:200])
        return _empty_extract()
    if not isinstance(data, dict):
        return _empty_extract()
    return {
        "tasks": [_norm_action(x) for x in data.get("tasks", []) if isinstance(x, dict)][:20],
        "future": [_norm_action(x) for x in data.get("future", []) if isinstance(x, dict)][:20],
        "decisions": [
            _norm_decision(x) for x in data.get("decisions", []) if isinstance(x, dict)
        ][:20],
        "tags": [str(t).strip()[:40] for t in data.get("tags", []) if t][:8],
    }


def _norm_action(x: dict) -> dict:
    title = str(x.get("title", "")).strip()[:200]
    due_hint = x.get("due_hint")
    if due_hint in ("", None, "null"):
        due_hint = None
    else:
        due_hint = str(due_hint).strip()[:40]
    return {"title": title, "due_hint": due_hint}


def _norm_decision(x: dict) -> dict:
    return {"summary": str(x.get("summary", "")).strip()[:300]}


# ── 2) Natural-language search across daily logs ──────────────────

_SEARCH_SYSTEM = (
    "너는 한국어 일기/업무 노트 아카이브를 검색하는 비서다.\n"
    "사용자가 자연어로 물어보면, 주어진 로그들에서 관련 내용을 찾아 답한다.\n\n"
    "답변 규칙:\n"
    "1. 반드시 JSON 으로만 응답한다. 코드블록 금지.\n"
    "2. 형식:\n"
    "   {\n"
    '     "answer": "사용자 질문에 대한 자연어 답변 (한국어, 2-5문장)",\n'
    '     "sources": [{"date":"YYYY-MM-DD","snippet":"관련 부분 60자 이내"}]\n'
    "   }\n"
    '3. 로그에서 관련 내용을 찾을 수 없으면 answer 에 "관련된 내용을 찾지 못했습니다." 라고 적고 sources 는 빈 배열.\n'
    "4. 답변은 추측이 아니라 실제 로그 내용에 기반해야 한다.\n"
    "5. 가장 관련성 높은 로그 3개 이내만 sources 에 포함."
)


def smart_search(query: str, logs: list[dict]) -> dict:
    """Ask Claude to answer `query` using the provided log list.

    `logs`: list of {log_date, content}, most-recent first.
    """
    if not query.strip():
        return {"answer": "", "sources": []}
    if not logs:
        return {"answer": "검색할 로그가 없습니다.", "sources": []}

    blocks: list[str] = []
    total = 0
    for log_row in logs:
        date = log_row.get("log_date") or ""
        content = (log_row.get("content") or "").strip()
        if not content:
            continue
        block = f"[{date}]\n{content}\n"
        if total + len(block) > MAX_INPUT_CHARS:
            break
        blocks.append(block)
        total += len(block)

    if not blocks:
        return {"answer": "로그가 비어있습니다.", "sources": []}

    context = "\n".join(blocks)
    user_prompt = (
        "=== 사용자의 하루 로그 모음 (최신순) ===\n"
        f"{context}\n"
        "=== 사용자 질문 ===\n"
        f"{query}\n\n"
        "위 JSON 형식으로만 응답해주세요."
    )

    msg = _client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=_SEARCH_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    _log_usage("search", msg)
    return _parse_search_json(_collect_text(msg))


def _parse_search_json(raw: str) -> dict:
    s = _strip_fence(raw)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        log.warning("ai.search JSON parse failed: %r", raw[:200])
        return {"answer": (raw or "응답을 해석하지 못했습니다.")[:500], "sources": []}
    if not isinstance(data, dict):
        return {"answer": "응답 형식이 올바르지 않습니다.", "sources": []}
    sources: list[dict] = []
    for src in data.get("sources", []):
        if not isinstance(src, dict):
            continue
        sources.append({
            "date": str(src.get("date", "")).strip()[:20],
            "snippet": str(src.get("snippet", "")).strip()[:300],
        })
    return {
        "answer": str(data.get("answer", "")).strip()[:3000],
        "sources": sources[:5],
    }
