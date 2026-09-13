"""RANKLAB 데모용 상품 조회 큐 (jisoar.com/sample ↔ 로컬 수집 워커 중계).

페이지가 스마트스토어 URL+키워드를 넣으면 큐에 쌓고, 로그인 세션 크롬이 도는 워커 PC가
큐를 가져가 상품명·상품ID·단일MID·키워드 순위를 채워 돌려준다. 메모리 큐(재시작 시 소멸, 데모용).

  POST /api/ranklab/lookup            {url, keyword}            → {id, status}
  GET  /api/ranklab/lookup/{id}                                 → {id, status, step, result?, error?}
  GET  /api/ranklab/worker/claim      (X-Worker-Key)            → 다음 대기 작업 1건 또는 204
  POST /api/ranklab/worker/progress   (X-Worker-Key) {id, step}
  POST /api/ranklab/worker/result     (X-Worker-Key) {id, ok, result|error}

워커 인증: SUPABASE_SERVICE_KEY 의 sha256 (백엔드·워커 PC 둘 다 같은 키를 이미 갖고 있어 새 비밀 불필요).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections import deque
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/ranklab", tags=["ranklab"])

# 워커 인증: RANKLAB_WORKER_TOKEN(전용 토큰) 우선, 없으면 SUPABASE_SERVICE_KEY 의 sha256
_WORKER_KEY = (os.getenv("RANKLAB_WORKER_TOKEN") or "").strip() or hashlib.sha256((os.getenv("SUPABASE_SERVICE_KEY") or "").encode()).hexdigest()
_JOBS: dict[str, dict] = {}
_QUEUE: deque[str] = deque()
_MAX_PENDING = 20
_JOB_TTL = 60 * 30
_RATE: dict[str, deque] = {}
_RATE_PER_MIN = 6
_NAVER_URL = re.compile(r"^https://([a-z0-9-]+\.)*naver\.com/[^\s]+$", re.I)
_PID = re.compile(r"/(?:window-)?products/(?:[^/?#]+/)?(\d+)")


class LookupIn(BaseModel):
    url: str
    keyword: str
    pages: Optional[int] = 13   # 순위 조회 최대 페이지(80개/페이지, 13=1,000위)


class ProgressIn(BaseModel):
    id: str
    step: str


class ResultIn(BaseModel):
    id: str
    ok: bool
    result: Optional[dict] = None
    error: Optional[str] = None


def _ip(request: Request) -> str:
    xf = request.headers.get("x-forwarded-for")
    return (xf.split(",")[0].strip() if xf else (request.client.host if request.client else "?"))


def _rate_ok(ip: str) -> bool:
    now = time.time()
    dq = _RATE.setdefault(ip, deque())
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= _RATE_PER_MIN:
        return False
    dq.append(now)
    return True


def _gc() -> None:
    now = time.time()
    for jid in [j for j, v in _JOBS.items() if now - v["created"] > _JOB_TTL]:
        _JOBS.pop(jid, None)
        try:
            _QUEUE.remove(jid)
        except ValueError:
            pass


def _check_worker(key: Optional[str]) -> None:
    if not key or not hmac.compare_digest(key, _WORKER_KEY):
        raise HTTPException(status_code=401, detail="worker key")


def _public(job: dict) -> dict:
    out = {k: job[k] for k in ("id", "status", "step", "created", "url", "keyword")}
    if job.get("result") is not None:
        out["result"] = job["result"]
    if job.get("error"):
        out["error"] = job["error"]
    out["queue_position"] = (list(_QUEUE).index(job["id"]) + 1) if job["id"] in _QUEUE else 0
    out["worker_seen"] = _LAST_CLAIM > 0 and (time.time() - _LAST_CLAIM) < 20
    return out


_LAST_CLAIM = 0.0


@router.post("/lookup")
def create_lookup(body: LookupIn, request: Request):
    _gc()
    if not _rate_ok(_ip(request)):
        raise HTTPException(status_code=429, detail="잠시 후 다시 시도해 주세요")
    url = (body.url or "").strip()
    kw = re.sub(r"\s+", " ", (body.keyword or "").strip())
    if not _NAVER_URL.match(url) or not _PID.search(url):
        raise HTTPException(status_code=400, detail="네이버 스토어 상품 상세 링크(…/products/상품ID)만 조회할 수 있습니다")
    if not (1 <= len(kw) <= 40):
        raise HTTPException(status_code=400, detail="키워드는 1~40자")
    if len(_QUEUE) >= _MAX_PENDING:
        raise HTTPException(status_code=503, detail="대기 중인 조회가 많습니다. 잠시 후 다시 시도해 주세요")
    jid = secrets.token_hex(8)
    _JOBS[jid] = {
        "id": jid, "status": "queued", "step": "대기 중", "created": time.time(),
        "url": url, "keyword": kw, "pages": max(1, min(13, int(body.pages or 13))),
        "pid": _PID.search(url).group(1), "result": None, "error": None,
    }
    _QUEUE.append(jid)
    return _public(_JOBS[jid])


@router.get("/lookup/{jid}")
def get_lookup(jid: str):
    job = _JOBS.get(jid)
    if not job:
        raise HTTPException(status_code=404, detail="없는 조회입니다(만료)")
    return _public(job)


@router.get("/worker/claim")
def worker_claim(x_worker_key: Optional[str] = Header(default=None)):
    global _LAST_CLAIM
    _check_worker(x_worker_key)
    _LAST_CLAIM = time.time()
    _gc()
    while _QUEUE:
        jid = _QUEUE.popleft()
        job = _JOBS.get(jid)
        if job and job["status"] == "queued":
            job["status"] = "running"
            job["step"] = "상품 페이지 여는 중"
            job["started"] = time.time()
            return {k: job[k] for k in ("id", "url", "keyword", "pages", "pid")}
    return Response(status_code=204)


@router.post("/worker/progress")
def worker_progress(body: ProgressIn, x_worker_key: Optional[str] = Header(default=None)):
    _check_worker(x_worker_key)
    job = _JOBS.get(body.id)
    if job and job["status"] == "running":
        job["step"] = body.step[:80]
    return {"ok": True}


@router.post("/worker/result")
def worker_result(body: ResultIn, x_worker_key: Optional[str] = Header(default=None)):
    _check_worker(x_worker_key)
    job = _JOBS.get(body.id)
    if not job:
        return {"ok": False}
    job["status"] = "done" if body.ok else "failed"
    job["step"] = "완료" if body.ok else "실패"
    job["result"] = body.result
    job["error"] = body.error
    job["finished"] = time.time()
    return {"ok": True}


@router.get("/recent")
def recent(key: Optional[str] = None, limit: int = 30):
    """최근 조회 내역 (워커 토큰을 key 로). 휴대폰에서 등록 여부 확인용."""
    _check_worker(key)
    jobs = sorted(_JOBS.values(), key=lambda j: j["created"], reverse=True)[: max(1, min(100, limit))]
    out = []
    for j in jobs:
        r = j.get("result") or {}
        out.append({
            "시각": time.strftime("%m-%d %H:%M:%S", time.localtime(j["created"] + 9 * 3600)),
            "상태": {"queued": "대기", "running": "조회중", "done": "완료", "failed": "실패"}.get(j["status"], j["status"]),
            "진행": j.get("step"), "키워드": j["keyword"], "url": j["url"],
            "상품명": r.get("name"), "상품ID": r.get("pid"), "단일MID": r.get("nvMid"),
            "순위": (r.get("rank") if r.get("found") else ("순위없음" if r else None)),
            "소요초": (round(r["elapsedMs"] / 1000) if r.get("elapsedMs") else None), "오류": j.get("error"),
        })
    return {"count": len(out), "jobs": out}


@router.get("/status")
def status():
    """워커 생존/큐 길이 (페이지가 '조회 서버 연결됨' 표시용)."""
    return {"worker_online": _LAST_CLAIM > 0 and (time.time() - _LAST_CLAIM) < 20, "pending": len(_QUEUE)}
