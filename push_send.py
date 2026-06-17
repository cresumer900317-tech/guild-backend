"""
일정 푸시 — Expo Push API 발송 + 스케줄러 job.

정책(콘텐츠당 회차별로 각 1회, 중복은 push_log 로 방지):
  · start    : 시작 순간 (시작 후 1시간 이내 첫 감지 시) — "지금 시작!"
  · last_day : 종료일 당일(KST) 아침 09시 이후 — "오늘이 마지막 날!"
  · end_3h   : 마감 3시간 전
  · end_1h   : 마감 1시간 전
대상: 전부(상시+시즌). push_tokens 의 모든 토큰에 발송.
탭하면 앱의 "이번 주 일정" 화면으로 이동(data.route="/schedule").
"""
from datetime import datetime, timedelta
import logging

import httpx

from database import supabase
from schedule_logic import build_schedule, KST

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _due_notifications(item: dict, now: datetime):
    """이 회차에서 '지금' 발송 대상인 (kind, title, body) 목록. 중복 제거는 호출측에서."""
    start = datetime.fromisoformat(item["startAt"]).astimezone(KST)
    end = datetime.fromisoformat(item["endAt"]).astimezone(KST)
    icon = item.get("icon") or "🔔"
    label = item["name"] + (f" {item['roundLabel']}" if item.get("roundLabel") else "")
    due = []

    # 1) 시작 순간 — 시작했고 시작 후 1시간 이내(배포 직후 지각 발송 방지)
    if start <= now < start + timedelta(hours=1):
        due.append(("start", f"{icon} {label} 시작!", f"지금 {label}이(가) 시작됐어요. 잊지 말고 참여하세요!"))

    # 종료 전 알림 (아직 안 끝난 것만)
    if now < end:
        remain = end - now
        # 2) 마지막 날 아침 — 여러 날에 걸친 회차 + 오늘이 종료일(KST) + 09시 이후
        if start.date() < end.date() and now.date() == end.date() and now.hour >= 9:
            due.append(("last_day", f"📅 오늘이 {label} 마지막 날!",
                        f"{label}이(가) 오늘 마감돼요. 아직이라면 오늘 안에 꼭 챙기세요!"))
        # 3) 마감 3시간 전
        if timedelta(hours=1) < remain <= timedelta(hours=3):
            due.append(("end_3h", f"🚨 {label} 마감 3시간 전!", "아직 안 했다면 서두르세요!"))
        # 4) 마감 1시간 전
        if remain <= timedelta(hours=1):
            due.append(("end_1h", f"⏰ {label} 마감 1시간 전!", "마지막 기회예요. 지금 바로!"))
    return due


def _send(tokens: list[str], title: str, body: str, data: dict):
    """Expo Push API 로 발송(100개씩 청크). DeviceNotRegistered 토큰은 정리."""
    messages = [{
        "to": t, "title": title, "body": body, "data": data,
        "sound": "default", "channelId": "default",
    } for t in tokens]
    with httpx.Client(timeout=15) as client:
        for i in range(0, len(messages), 100):
            chunk = messages[i:i + 100]
            try:
                resp = client.post(EXPO_PUSH_URL, json=chunk,
                                   headers={"Content-Type": "application/json"})
                tickets = (resp.json() or {}).get("data", [])
                for msg, ticket in zip(chunk, tickets):
                    if (ticket.get("status") == "error"
                            and (ticket.get("details") or {}).get("error") == "DeviceNotRegistered"):
                        supabase.table("push_tokens").delete().eq("token", msg["to"]).execute()
            except Exception as e:
                logger.error(f"[일정푸시] Expo 발송 실패: {e}")


def run_schedule_push():
    """스케줄러가 5분마다 호출. 발송 시점이 된 일정 알림을 1회씩 발송."""
    try:
        tokens = [r["token"] for r in
                  (supabase.table("push_tokens").select("token").execute().data or [])]
        if not tokens:
            return
        now = datetime.now(KST)
        sent = 0
        for item in build_schedule(weeks=1):
            key = str(item["id"])
            for kind, title, body in _due_notifications(item, now):
                # 중복 방지 — 이미 보낸 (회차, 종류) 면 skip
                already = (supabase.table("push_log").select("id")
                           .eq("occurrence_key", key).eq("kind", kind).limit(1)
                           .execute().data)
                if already:
                    continue
                _send(tokens, title, body, {"route": "/schedule"})
                supabase.table("push_log").insert({"occurrence_key": key, "kind": kind}).execute()
                sent += 1
                logger.info(f"[일정푸시] 발송 {key}/{kind} → {len(tokens)} tokens")
        if sent:
            logger.info(f"[일정푸시] 이번 사이클 {sent}건 발송")
    except Exception as e:
        logger.error(f"[일정푸시] 오류: {e}")
