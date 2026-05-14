"""
매일 아침 이메일 디지스트.

- 데이터 출처: Supabase (personal_tasks, personal_projects, personal_inbox, personal_daily_logs)
- 발송: Resend API (https://resend.com/docs/api-reference)
- 트리거: scheduler.py 의 CronTrigger (08:00 KST)
- 수신자/소유자/발신자: 모두 환경변수
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import requests

from database import supabase

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
RESEND_ENDPOINT = "https://api.resend.com/emails"

DIGEST_OWNER = os.getenv("DIGEST_OWNER", "친구닷")
DIGEST_RECIPIENT = os.getenv("DIGEST_RECIPIENT_EMAIL")
DIGEST_FROM = os.getenv("DIGEST_FROM_EMAIL", "내 업무 <onboarding@resend.dev>")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")

PRIORITY_LABEL = {"high": "높음", "medium": "보통", "low": "낮음"}
PRIORITY_BADGE = {
    "high": ("#fee2e2", "#b91c1c"),
    "medium": ("#fef3c7", "#92400e"),
    "low": ("#dcfce7", "#166534"),
}


def _today_kst() -> date:
    return datetime.now(KST).date()


def _fetch_active_tasks(owner: str) -> list[dict[str, Any]]:
    """완료되지 않은 모든 업무."""
    res = (
        supabase.table("personal_tasks")
        .select("*")
        .eq("owner", owner)
        .neq("status", "done")
        .order("due_date", desc=False)
        .order("priority", desc=False)
        .execute()
    )
    return res.data or []


def _fetch_active_projects(owner: str) -> list[dict[str, Any]]:
    res = (
        supabase.table("personal_projects")
        .select("*")
        .eq("owner", owner)
        .eq("status", "active")
        .order("end_date", desc=False)
        .execute()
    )
    return res.data or []


def _fetch_active_inbox(owner: str) -> list[dict[str, Any]]:
    res = (
        supabase.table("personal_inbox")
        .select("*")
        .eq("owner", owner)
        .eq("processed", False)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def _fetch_recent_daily_log(owner: str) -> dict[str, Any] | None:
    """오늘 또는 가장 최근 daily log 1개."""
    res = (
        supabase.table("personal_daily_logs")
        .select("*")
        .eq("owner", owner)
        .order("log_date", desc=True)
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


def _completed_today_count(owner: str, today: date) -> int:
    """오늘 완료된 업무 수 (completed_at 기준)."""
    start = datetime.combine(today, datetime.min.time(), tzinfo=KST).isoformat()
    res = (
        supabase.table("personal_tasks")
        .select("id", count="exact")
        .eq("owner", owner)
        .eq("status", "done")
        .gte("completed_at", start)
        .execute()
    )
    return res.count or 0


def _d_day_label(target: date | None, today: date) -> str:
    if not target:
        return ""
    d = (target - today).days
    if d == 0:
        return "D-DAY"
    if d > 0:
        return f"D-{d}"
    return f"D+{-d}"


def _task_row_html(t: dict[str, Any], today: date) -> str:
    title = escape(t.get("title") or "")
    cat = t.get("category") or ""
    cat_html = (
        f'<span style="display:inline-block;padding:2px 8px;margin-left:8px;'
        f'border-radius:999px;font-size:11px;background:#eef2ff;color:#4338ca;">#{escape(cat)}</span>'
        if cat
        else ""
    )
    prio = t.get("priority") or "medium"
    bg, fg = PRIORITY_BADGE.get(prio, PRIORITY_BADGE["medium"])
    prio_html = (
        f'<span style="display:inline-block;padding:2px 8px;margin-left:6px;'
        f'border-radius:999px;font-size:11px;background:{bg};color:{fg};">{PRIORITY_LABEL[prio]}</span>'
    )
    due_html = ""
    due_str = t.get("due_date")
    if due_str:
        try:
            due_d = date.fromisoformat(due_str)
            label = _d_day_label(due_d, today)
            color = "#b91c1c" if due_d < today else ("#92400e" if due_d == today else "#475569")
            due_html = (
                f'<span style="float:right;font-size:12px;color:{color};font-weight:600;">'
                f'{due_d.month}/{due_d.day} · {label}</span>'
            )
        except ValueError:
            pass
    return (
        f'<li style="padding:10px 0;border-bottom:1px solid #e2e8f0;list-style:none;">'
        f'<div>{due_html}<span style="font-weight:500;color:#0f172a;">{title}</span>{cat_html}{prio_html}</div>'
        f"</li>"
    )


def _section(title: str, count: int, body_html: str) -> str:
    return (
        f'<div style="margin-bottom:28px;">'
        f'<h2 style="margin:0 0 10px;font-size:15px;color:#334155;font-weight:600;">'
        f'{escape(title)} <span style="color:#94a3b8;font-weight:500;">({count})</span></h2>'
        f'<ul style="margin:0;padding:0;">{body_html}</ul>'
        f"</div>"
    )


def _empty_li(msg: str) -> str:
    return f'<li style="padding:10px 0;color:#94a3b8;font-size:13px;list-style:none;">{escape(msg)}</li>'


def build_digest(owner: str) -> dict[str, Any]:
    """디지스트 데이터 수집 + HTML 렌더."""
    today = _today_kst()
    tomorrow = today + timedelta(days=1)
    in_3d = today + timedelta(days=3)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    today_label = f"{today.month}월 {today.day}일 ({weekdays[today.weekday()]})"

    tasks = _fetch_active_tasks(owner)
    projects = _fetch_active_projects(owner)
    inbox = _fetch_active_inbox(owner)
    recent_log = _fetch_recent_daily_log(owner)
    completed_today = _completed_today_count(owner, today)

    def _due(t):
        ds = t.get("due_date")
        if not ds:
            return None
        try:
            return date.fromisoformat(ds)
        except ValueError:
            return None

    overdue = [t for t in tasks if (d := _due(t)) and d < today]
    today_tasks = [t for t in tasks if _due(t) == today]
    tomorrow_tasks = [t for t in tasks if _due(t) == tomorrow]
    upcoming = [t for t in tasks if (d := _due(t)) and today < d <= in_3d and d != tomorrow]
    no_due = [t for t in tasks if not _due(t)]

    # ── HTML 본문 ────────────────────────────────────────
    parts = []

    parts.append(
        f'<div style="margin-bottom:20px;">'
        f'<div style="font-size:13px;color:#64748b;">오늘의 브리핑 · {today_label}</div>'
        f'<div style="margin-top:8px;font-size:14px;color:#475569;">'
        f"오늘 마감 <b>{len(today_tasks)}</b> · 지연 <b style=\"color:#b91c1c;\">{len(overdue)}</b> · "
        f"내일 <b>{len(tomorrow_tasks)}</b> · 어제까지 완료 <b>{completed_today}</b>"
        f"</div></div>"
    )

    if overdue:
        body = "".join(_task_row_html(t, today) for t in overdue)
        parts.append(_section("⚠ 마감 지난 일", len(overdue), body))

    body = "".join(_task_row_html(t, today) for t in today_tasks) or _empty_li("오늘 마감인 업무가 없습니다")
    parts.append(_section("오늘 마감", len(today_tasks), body))

    if tomorrow_tasks:
        body = "".join(_task_row_html(t, today) for t in tomorrow_tasks)
        parts.append(_section("내일 마감", len(tomorrow_tasks), body))

    if upcoming:
        body = "".join(_task_row_html(t, today) for t in upcoming)
        parts.append(_section("D-3 이내", len(upcoming), body))

    if projects:
        rows = []
        for p in projects:
            name = escape(p.get("name") or "")
            color = escape(p.get("color") or "#6366f1")
            pct = p.get("progress_pct") or 0
            try:
                end_d = date.fromisoformat(p["end_date"]) if p.get("end_date") else None
            except ValueError:
                end_d = None
            d_label = _d_day_label(end_d, today) if end_d else ""
            d_html = (
                f'<span style="float:right;font-size:12px;color:#64748b;">{d_label}</span>'
                if d_label
                else ""
            )
            rows.append(
                f'<li style="padding:10px 0;border-bottom:1px solid #e2e8f0;list-style:none;">'
                f'{d_html}<span style="display:inline-block;width:8px;height:8px;border-radius:50%;'
                f'background:{color};margin-right:8px;vertical-align:middle;"></span>'
                f'<span style="font-weight:500;color:#0f172a;">{name}</span> '
                f'<span style="color:#94a3b8;font-size:12px;">· {pct}%</span>'
                f'<div style="margin-top:6px;height:5px;background:#e2e8f0;border-radius:3px;overflow:hidden;">'
                f'<div style="height:100%;width:{pct}%;background:{color};"></div></div>'
                f"</li>"
            )
        parts.append(_section("진행 중 프로젝트", len(projects), "".join(rows)))

    if inbox:
        rows = []
        for it in inbox[:20]:
            content = escape((it.get("content") or "")[:200])
            rows.append(
                f'<li style="padding:8px 0;border-bottom:1px solid #e2e8f0;list-style:none;'
                f'font-size:13px;color:#475569;">• {content}</li>'
            )
        more = (
            f'<li style="padding:8px 0;color:#94a3b8;font-size:12px;list-style:none;">'
            f"... 외 {len(inbox) - 20}개</li>"
            if len(inbox) > 20
            else ""
        )
        parts.append(_section("미처리 메모(Inbox)", len(inbox), "".join(rows) + more))

    if no_due:
        rows = []
        for t in no_due[:10]:
            rows.append(_task_row_html(t, today))
        more = (
            f'<li style="padding:8px 0;color:#94a3b8;font-size:12px;list-style:none;">'
            f"... 외 {len(no_due) - 10}개</li>"
            if len(no_due) > 10
            else ""
        )
        parts.append(_section("마감 미정", len(no_due), "".join(rows) + more))

    if recent_log and (recent_log.get("content") or "").strip():
        log_date = recent_log.get("log_date") or ""
        log_content = escape(recent_log["content"][:500])
        parts.append(
            f'<div style="margin-top:32px;padding:16px;background:#f8fafc;border-radius:8px;'
            f'border-left:3px solid #6366f1;">'
            f'<div style="font-size:12px;color:#64748b;margin-bottom:6px;">최근 하루 로그 · {escape(log_date)}</div>'
            f'<div style="font-size:13px;color:#334155;white-space:pre-wrap;line-height:1.6;">{log_content}</div>'
            f"</div>"
        )

    body_html = (
        f'<div style="max-width:600px;margin:0 auto;padding:24px 20px;'
        f'font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",\"Apple SD Gothic Neo\","Noto Sans KR",sans-serif;'
        f'background:#ffffff;color:#0f172a;">'
        f'<h1 style="margin:0 0 4px;font-size:22px;font-weight:700;">내 업무</h1>'
        + "".join(parts)
        + f'<div style="margin-top:32px;padding-top:16px;border-top:1px solid #e2e8f0;'
        f'font-size:12px;color:#94a3b8;text-align:center;">'
        f'<a href="https://친구들.com/me" style="color:#6366f1;text-decoration:none;">친구들.com/me</a> 에서 확인</div>'
        f"</div>"
    )

    subject = f"[내 업무] {today_label} · 오늘 {len(today_tasks)} · 지연 {len(overdue)}"
    return {"subject": subject, "html": body_html}


def send_digest(owner: str = None) -> dict[str, Any]:
    owner = owner or DIGEST_OWNER
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY 가 설정되지 않았습니다")
    if not DIGEST_RECIPIENT:
        raise RuntimeError("DIGEST_RECIPIENT_EMAIL 가 설정되지 않았습니다")

    digest = build_digest(owner)
    payload = {
        "from": DIGEST_FROM,
        "to": [DIGEST_RECIPIENT],
        "subject": digest["subject"],
        "html": digest["html"],
    }
    resp = requests.post(
        RESEND_ENDPOINT,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if resp.status_code >= 300:
        logger.error("[디지스트] Resend 응답 실패 %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
    data = resp.json()
    logger.info("[디지스트] 전송 완료 id=%s to=%s", data.get("id"), DIGEST_RECIPIENT)
    return data


def run_daily_digest() -> None:
    """APScheduler 가 호출하는 진입점."""
    logger.info("=== [디지스트] 일일 발송 시작 ===")
    try:
        send_digest(DIGEST_OWNER)
        logger.info("=== [디지스트] 완료 ===")
    except Exception as e:
        logger.exception("[디지스트] 발송 실패: %s", e)
