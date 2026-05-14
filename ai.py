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
CLASSIFY_KIND = "inbox_classify"
BRIEFING_KIND = "dashboard_briefing"
DAILY_TEMPLATE_KIND = "daily_template"


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


# ── 3) Inbox classification (Phase 6d) ────────────────────────────

_CLASSIFY_SYSTEM = (
    "너는 한국어로 쓰인 짧은 메모/Inbox 항목을 정리하는 비서다.\n"
    "주어진 메모와 사용자의 카테고리 목록을 보고, 다음을 추론한다:\n\n"
    "1. suggested_title: 메모를 '할 일 제목'으로 깔끔히 다듬은 한 줄 (30자 이내).\n"
    "   원문이 이미 짧고 깔끔한 제목이면 그대로 사용. 추측해서 새 내용 추가 금지.\n"
    "2. suggested_category: 사용자 카테고리 목록 중 가장 어울리는 이름 1개.\n"
    "   확실하지 않거나 어떤 것에도 안 맞으면 null.\n"
    "3. suggested_priority: high | medium | low 중 하나.\n"
    "   - high: '긴급', '오늘까지', '내일까지', '중요', 명백히 시간이 촉박한 경우.\n"
    "   - low: 단순 메모, 아이디어, 참고용, 일회성 기록.\n"
    "   - medium: 그 외 기본값.\n"
    "4. suggested_tags: 메모에 등장하는 핵심 키워드 (프로젝트명/사람 이름/주제). 최대 3개. 없으면 빈 배열.\n\n"
    "반드시 JSON 으로만 응답. 코드블록 금지. 형식:\n"
    "{\n"
    '  "suggested_title": "...",\n'
    '  "suggested_category": "..." | null,\n'
    '  "suggested_priority": "high" | "medium" | "low",\n'
    '  "suggested_tags": ["...", ...]\n'
    "}\n"
)


def classify_inbox_item(content: str, categories: list[str]) -> dict:
    """Classify a single inbox memo into title/category/priority/tags suggestions.

    `categories`: list of category names that the user has defined.
    Returns dict with the four suggested_* fields. Caller handles caching.
    """
    text = (content or "").strip()
    if not text:
        return _empty_classify()

    if len(text) > 2000:
        text = text[:2000]

    cat_block = (
        "사용자 카테고리 목록: " + ", ".join(categories) + "\n"
        if categories else "사용자 카테고리 목록: (없음)\n"
    )
    user_prompt = (
        f"{cat_block}\n"
        "다음은 사용자가 적은 메모입니다:\n---\n"
        f"{text}\n---\n\n"
        "위 JSON 형식으로만 응답해주세요."
    )

    msg = _client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=400,
        system=_CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    _log_usage("classify", msg)
    return _parse_classify_json(_collect_text(msg), categories)


def _empty_classify() -> dict:
    return {
        "suggested_title": "",
        "suggested_category": None,
        "suggested_priority": "medium",
        "suggested_tags": [],
    }


def _parse_classify_json(raw: str, categories: list[str]) -> dict:
    s = _strip_fence(raw)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        log.warning("ai.classify JSON parse failed: %r", raw[:200])
        return _empty_classify()
    if not isinstance(data, dict):
        return _empty_classify()

    title = str(data.get("suggested_title", "")).strip()[:60]

    cat_raw = data.get("suggested_category")
    cat = None
    if cat_raw and isinstance(cat_raw, str):
        cat_s = cat_raw.strip()
        # 모델이 카테고리 외 값을 만들어내면 무시 (사용자 정의 외에는 안 받음)
        if cat_s and cat_s in set(categories):
            cat = cat_s

    prio_raw = str(data.get("suggested_priority", "medium")).strip().lower()
    if prio_raw not in ("high", "medium", "low"):
        prio_raw = "medium"

    tags_raw = data.get("suggested_tags") or []
    tags: list[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw[:6]:
            if not t:
                continue
            tag = str(t).strip()[:30]
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= 3:
                break

    return {
        "suggested_title": title,
        "suggested_category": cat,
        "suggested_priority": prio_raw,
        "suggested_tags": tags,
    }


# ── 4) Dashboard briefing (Phase 7) ────────────────────────────────

_BRIEFING_SYSTEM = (
    "너는 사용자의 오늘 업무 상태를 1~3문장 한국어로 요약하는 비서다.\n"
    "주어진 숫자(오늘 마감 / 미처리 메모 / 진행 중 프로젝트 / 위험 task) 와 "
    "최근 하루 로그·진행 중 task 일부를 보고, 친절하지만 간결하게 한 단락으로 답한다.\n\n"
    "규칙:\n"
    "1. 1~3문장. 절대 4문장 넘지 말 것.\n"
    "2. 절대 데이터 없는 내용 만들어내지 말 것. 모르면 짧게 '오늘 큰 일정은 없어요' 정도.\n"
    "3. 이모지는 0~1개만. 과하지 않게.\n"
    "4. 평어체로 따뜻하게.\n"
    "5. JSON 등 형식 없이 그냥 한 단락 텍스트로만 응답.\n"
)


def dashboard_briefing(stats: dict, sample: dict) -> str:
    """Generate a 1-3 sentence dashboard briefing.

    `stats`: {today_due, inbox_unprocessed, projects_active, at_risk}
    `sample`: {today_tasks: [..titles..], recent_log_excerpt: str}
    Returns plain Korean text (no JSON).
    """
    payload = {
        "today_due": int(stats.get("today_due", 0)),
        "inbox_unprocessed": int(stats.get("inbox_unprocessed", 0)),
        "projects_active": int(stats.get("projects_active", 0)),
        "at_risk": int(stats.get("at_risk", 0)),
        "today_tasks": [str(t)[:60] for t in (sample.get("today_tasks") or [])][:8],
        "recent_log_excerpt": (sample.get("recent_log_excerpt") or "")[:600],
    }
    user_prompt = (
        "오늘 상태:\n"
        f"- 오늘 마감 또는 지난 task: {payload['today_due']}개\n"
        f"- 미처리 메모(Inbox): {payload['inbox_unprocessed']}개\n"
        f"- 진행 중 프로젝트: {payload['projects_active']}개\n"
        f"- 위험(우선순위 high & 마감 D-3 이내): {payload['at_risk']}개\n"
    )
    if payload["today_tasks"]:
        user_prompt += "\n오늘 task 일부:\n" + "\n".join(
            f"- {t}" for t in payload["today_tasks"]
        ) + "\n"
    if payload["recent_log_excerpt"]:
        user_prompt += (
            "\n최근 하루 로그 발췌:\n---\n"
            f"{payload['recent_log_excerpt']}\n---\n"
        )
    user_prompt += "\n위 정보를 보고 1~3문장 한국어로 오늘 상태를 친절하게 요약해주세요."

    msg = _client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=_BRIEFING_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    _log_usage("briefing", msg)
    text = _collect_text(msg)
    if len(text) > 600:
        text = text[:600].rstrip() + "…"
    return text


# ── 5) Daily log auto-template (Phase 7) ───────────────────────────

def daily_auto_template(today_str: str, completed_titles: list[str],
                        today_due_titles: list[str],
                        active_project_names: list[str]) -> str:
    """Build a deterministic daily-log starter template — no AI call needed.

    Lightweight scaffolding so users see a useful editable draft when opening
    today's empty log. Pure Python so it works even when ANTHROPIC_API_KEY
    is missing.
    """
    lines: list[str] = []

    lines.append("# 오늘 완료한 일")
    if completed_titles:
        for t in completed_titles[:20]:
            lines.append(f"- {t}")
    else:
        lines.append("- ")
    lines.append("")

    lines.append("# 오늘 일정")
    if today_due_titles:
        for t in today_due_titles[:20]:
            lines.append(f"- {t}")
    else:
        lines.append("- ")
    lines.append("")

    if active_project_names:
        lines.append("# 진행 중 프로젝트")
        for n in active_project_names[:8]:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("# 회고 (선택)")
    lines.append("- 잘된 것: ")
    lines.append("- 아쉬운 것: ")
    lines.append("- 내일 우선: ")

    return "\n".join(lines)


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
