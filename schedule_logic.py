"""
콘텐츠 일정 빌드 로직 (단일 소스).

`GET /api/schedule`(main.py)와 일정 푸시 스케줄러(push_send.py)가 동일한 회차 계산을
쓰도록 분리한 모듈. FastAPI에 의존하지 않는다(supabase + 표준 라이브러리만).
설계: guild-app-54/docs/일정_백엔드화_설계.md · 게임 시각은 전부 KST.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from database import supabase

KST = ZoneInfo("Asia/Seoul")


def _expand_rule(rule: dict, window_start: datetime, window_end: datetime):
    """상시 콘텐츠 주간 규칙을 [window_start, window_end] 구간 회차로 펼친다.
    rule.weekday 는 JS getDay 컨벤션(0=일..6=토). Python weekday()는 0=월..6=일 → 변환."""
    try:
        target = (int(rule["weekday"]) + 6) % 7  # JS getDay → Python weekday()
        dur = timedelta(minutes=int(rule.get("durationMin", 0)))
        hour = int(rule.get("hour", 0)); minute = int(rule.get("min", 0))
    except (KeyError, TypeError, ValueError):
        return []
    # window_start 1주 전의 target 요일부터 시작 → 진행 중인 회차도 포함
    base = (window_start - timedelta(days=7)).astimezone(KST).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    base = base + timedelta(days=(target - base.weekday()) % 7)
    out = []
    cur = base
    while cur <= window_end:
        start, end = cur, cur + dur
        if end >= window_start:  # 진행 중 + 미래만
            out.append((start, end))
        cur = cur + timedelta(days=7)
    return out


def build_schedule(weeks: int = 6) -> list[dict]:
    """상시(규칙 펼침) + 시즌(occurrences) 병합 회차 리스트(camelCase).
    id: 시즌=int, 상시=합성문자열(content@start). startAt/endAt: ISO(KST 오프셋 포함)."""
    weeks = max(1, min(weeks, 26))
    now = datetime.now(KST)
    window_start = now - timedelta(days=7)
    window_end = now + timedelta(weeks=weeks)

    contents = (supabase.table("contents").select("*")
                .eq("active", True).order("sort_order").execute().data) or []
    meta = {c["id"]: c for c in contents}

    items = []
    # ① 상시: 규칙으로 펼침
    for c in contents:
        if c.get("type") == "always" and c.get("recurrence"):
            for start, end in _expand_rule(c["recurrence"], window_start, window_end):
                items.append({
                    "id": f"{c['id']}@{start.isoformat()}",
                    "contentId": c["id"], "name": c["name"], "icon": c["icon"],
                    "type": "always", "roundLabel": None,
                    "startAt": start.isoformat(), "endAt": end.isoformat(),
                })

    # ② 시즌: occurrences 테이블 (구간 겹치는 것만)
    occ = (supabase.table("occurrences").select("*")
           .gte("end_at", window_start.isoformat())
           .lte("start_at", window_end.isoformat())
           .order("start_at").execute().data) or []
    for o in occ:
        c = meta.get(o["content_id"])
        if not c:
            continue
        items.append({
            "id": o["id"],
            "contentId": o["content_id"], "name": c["name"], "icon": c["icon"],
            "type": c["type"], "roundLabel": o.get("round_label"),
            "startAt": o["start_at"], "endAt": o["end_at"],
        })

    items.sort(key=lambda x: x["startAt"])
    return items
