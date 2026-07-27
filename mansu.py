"""만수로그 — 반려묘 박만수 몸무게 기록 API (jisoar.com/mansu).

- 조회는 공개 (고양이 몸무게라 민감정보 아님)
- 기록/삭제는 MANSU_ADMIN_TOKEN 으로만 (미설정 시 WEDDING_ADMIN_TOKEN 재사용)
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from database import supabase

router = APIRouter(prefix="/api/mansu", tags=["mansu"])

_KST = timezone(timedelta(hours=9))
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_key(key: Optional[str]):
    tok = (os.environ.get("MANSU_ADMIN_TOKEN") or os.environ.get("WEDDING_ADMIN_TOKEN") or "").strip()
    if not tok or not key or key.strip() != tok:
        raise HTTPException(status_code=403, detail="기록 권한이 없습니다")


class EntryIn(BaseModel):
    grams: int
    date: Optional[str] = None  # YYYY-MM-DD (KST), 생략 시 오늘
    memo: Optional[str] = ""


@router.get("/entries")
def list_entries():
    res = supabase.table("mansu_log").select("date,grams,memo").order("date", desc=False).execute()
    return {"entries": res.data or []}


@router.post("/entries")
def upsert_entry(body: EntryIn, x_mansu_key: Optional[str] = Header(None)):
    _check_key(x_mansu_key)
    if not (1 <= body.grams <= 20000):
        raise HTTPException(status_code=400, detail="몸무게(g)가 이상해요")
    date = (body.date or "").strip() or datetime.now(_KST).date().isoformat()
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="날짜는 YYYY-MM-DD 형식으로")
    row = {
        "date": date,
        "grams": body.grams,
        "memo": (body.memo or "").strip()[:200],
        "updated_at": datetime.now(_KST).isoformat(),
    }
    supabase.table("mansu_log").upsert(row, on_conflict="date").execute()
    return {"status": "ok", "date": date, "grams": body.grams}


@router.delete("/entries/{date}")
def delete_entry(date: str, x_mansu_key: Optional[str] = Header(None)):
    _check_key(x_mansu_key)
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="날짜는 YYYY-MM-DD 형식으로")
    supabase.table("mansu_log").delete().eq("date", date).execute()
    return {"status": "ok"}
