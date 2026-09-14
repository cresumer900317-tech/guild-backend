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
_MAX_PENDING = 60
_JOB_TTL = 60 * 60
_RATE: dict[str, deque] = {}
_RATE_PER_MIN = 10
_NAVER_URL = re.compile(r"^https://([a-z0-9-]+\.)*naver\.com/[^\s]+$", re.I)
_PID = re.compile(r"/(?:window-)?products/(?:[^/?#]+/)?(\d+)|/catalog/(\d+)")   # 상품 상세 또는 가격비교(카탈로그)


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
        raise HTTPException(status_code=400, detail="네이버 상품 상세 링크(…/products/상품ID) 또는 가격비교 링크(…/catalog/MID)만 조회할 수 있습니다")
    if not (1 <= len(kw) <= 40):
        raise HTTPException(status_code=400, detail="키워드는 1~40자")
    if len(_QUEUE) >= _MAX_PENDING:
        raise HTTPException(status_code=503, detail="대기 중인 조회가 많습니다. 잠시 후 다시 시도해 주세요")
    jid = secrets.token_hex(8)
    _JOBS[jid] = {
        "id": jid, "status": "queued", "step": "대기 중", "created": time.time(),
        "url": url, "keyword": kw, "pages": max(1, min(13, int(body.pages or 13))),
        "pid": _PID.search(url).group(1) or "", "catalogId": _PID.search(url).group(2) or "", "result": None, "error": None,
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
    if not _QUEUE:
        try:
            _enqueue_retry()
        except Exception as e:
            print(f"[ranklab] retry enqueue failed: {e}")
    while _QUEUE:
        jid = _QUEUE.popleft()
        job = _JOBS.get(jid)
        if job and job["status"] == "queued":
            job["status"] = "running"
            job["step"] = "상품 페이지 여는 중"
            job["started"] = time.time()
            return {k: job[k] for k in ("id", "url", "keyword", "pages", "pid", "catalogId")}
    return Response(status_code=204)


# ── 미완성 항목 자동 재확인 ───────────────────────────────────────
# 상품명·MID 를 못 찾았거나 즉시 조회가 실패한 슬롯은 30분 간격으로 최대 3회 다시 조회한다.
# 워커가 놀고 있을 때만 1건씩 큐에 넣으므로 사용자 등록이 항상 우선. 메모리 부담은 슬롯당 두 필드(tries, lastTry)뿐.
_RETRY_AFTER = 30 * 60
_RETRY_MAX = 3


def _needs_retry(s: dict) -> bool:
    if not s.get("user") or s.get("live") or not s.get("url") or not _NAVER_URL.match(s.get("url") or "") or not _PID.search(s.get("url") or ""):
        return False
    if int(s.get("tries") or 0) >= _RETRY_MAX:
        return False
    if s.get("status") == "wait":
        return True
    name = str(s.get("name") or "")
    mid = str(s.get("nvMid") or "")
    return s.get("status") in ("ok", "err") and (not name or name.startswith("조회 중") or "(수집 대기)" in name or mid in ("", "-"))


def _enqueue_retry() -> None:
    now = time.time()
    with _STATE_LOCK:
        _load_state()
        cands = [s for s in _STATE["slots"] if _needs_retry(s) and now - float(s.get("lastTry") or 0) > _RETRY_AFTER]
        if not cands:
            return
        # 같은 url+키워드는 한 작업으로 묶는다
        first = cands[0]
        group = [s for s in cands if s.get("url") == first.get("url") and s.get("kw") == first.get("kw")]
        m = _PID.search(first["url"])
        jid = secrets.token_hex(8)
        _JOBS[jid] = {
            "id": jid, "status": "queued", "step": "자동 재확인 대기", "created": now,
            "url": first["url"], "keyword": first["kw"], "pages": 13,
            "pid": (m.group(1) or "") if m else "", "catalogId": (m.group(2) or "") if m else "", "result": None, "error": None, "retry": True,
        }
        _QUEUE.append(jid)
        for s in group:
            s.update({"jobId": jid, "live": True, "tries": int(s.get("tries") or 0) + 1, "lastTry": now,
                      "note": f"자동 재확인 {int(s.get('tries') or 0) + 1}/{_RETRY_MAX} 진행 중"})
        _STATE["version"] += 1
        _save_state()
        print(f"[ranklab] retry enqueued {jid} for {len(group)} slot(s): {first['kw']}")


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
    try:
        _apply_result_to_slots(job)
    except Exception as e:
        print(f"[ranklab] apply result failed: {e}")
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


# ══════════════════════════════════════════════════════════════════════
# 공용 등록 목록(모든 사용자가 같은 화면) — Supabase Storage 의 JSON 파일에 저장
#   버킷 ranklab(비공개) / demo/state.json = {"seq": int, "slots": [...], "logs": [...]}
#   SQL 없이 서비스키만으로 동작. 백엔드 메모리에 캐시하고 변경 때마다 저장.
# ══════════════════════════════════════════════════════════════════════
import json
import threading

import httpx

_SB_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
_SB_KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
_BUCKET = "ranklab"
_OBJECT = "demo/state.json"
_STATE: dict = {"seq": 290000, "slots": [], "logs": [], "hidden": [], "version": 0}
_STATE_LOCK = threading.Lock()
_STATE_LOADED = False


def _sb_headers(extra: Optional[dict] = None) -> dict:
    h = {"Authorization": f"Bearer {_SB_KEY}", "apikey": _SB_KEY}
    if extra:
        h.update(extra)
    return h


def _ensure_bucket() -> None:
    try:
        r = httpx.get(f"{_SB_URL}/storage/v1/bucket/{_BUCKET}", headers=_sb_headers(), timeout=10)
        if r.status_code == 200:
            return
        httpx.post(f"{_SB_URL}/storage/v1/bucket", headers=_sb_headers({"content-type": "application/json"}),
                   json={"id": _BUCKET, "name": _BUCKET, "public": False}, timeout=10)
    except Exception as e:
        print(f"[ranklab] bucket check failed: {e}")


def _load_state() -> None:
    global _STATE, _STATE_LOADED
    if _STATE_LOADED or not _SB_URL or not _SB_KEY:
        _STATE_LOADED = True
        return
    _ensure_bucket()
    try:
        r = httpx.get(f"{_SB_URL}/storage/v1/object/{_BUCKET}/{_OBJECT}", headers=_sb_headers(), timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and isinstance(data.get("slots"), list):
                _STATE = {"seq": int(data.get("seq") or 290000), "slots": data["slots"], "logs": data.get("logs") or [], "hidden": data.get("hidden") or [], "version": int(data.get("version") or 0)}
                print(f"[ranklab] state loaded: {len(_STATE['slots'])} slots")
    except Exception as e:
        print(f"[ranklab] state load failed: {e}")
    _STATE_LOADED = True


def _save_state() -> None:
    if not _SB_URL or not _SB_KEY:
        return
    try:
        body = json.dumps(_STATE, ensure_ascii=False).encode("utf-8")
        r = httpx.post(f"{_SB_URL}/storage/v1/object/{_BUCKET}/{_OBJECT}",
                       headers=_sb_headers({"content-type": "application/json", "x-upsert": "true"}), content=body, timeout=15)
        if r.status_code >= 300:
            print(f"[ranklab] state save failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[ranklab] state save failed: {e}")


class SlotIn(BaseModel):
    owner: str
    kw: str
    url: str = ""
    days: int = 30
    qty: int = 1
    pid: str = ""
    nvMid: str = ""
    name: str = ""
    mall: str = ""
    catalog: bool = False
    price: Optional[int] = None
    review: Optional[int] = None
    status: str = "wait"          # ok | err | wait
    rank: Optional[int] = None
    note: str = ""
    jobId: Optional[str] = None
    start: str = ""
    end: str = ""


def _kst_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 9 * 3600))


@router.get("/state")
def get_state():
    """공용 등록 목록(모든 브라우저가 같은 화면)."""
    with _STATE_LOCK:
        _load_state()
        return {"version": _STATE["version"], "slots": _STATE["slots"], "logs": _STATE["logs"], "hidden": _STATE.get("hidden") or []}


@router.post("/slots")
def add_slots(body: SlotIn, request: Request):
    if not _rate_ok(_ip(request)):
        raise HTTPException(status_code=429, detail="잠시 후 다시 시도해 주세요")
    kw = re.sub(r"\s+", " ", body.kw.strip())
    if not (1 <= len(kw) <= 40) or not body.owner.strip():
        raise HTTPException(status_code=400, detail="키워드/광고주 확인")
    qty = max(1, min(100, body.qty))
    days = max(1, min(365, body.days))
    with _STATE_LOCK:
        _load_state()
        created = []
        for _ in range(qty):
            _STATE["seq"] += 1
            slot = body.model_dump()
            slot.update({"no": _STATE["seq"], "kw": kw, "qty": 1, "days": days, "user": True, "tries": 0, "lastTry": time.time(),
                         "live": bool(body.jobId), "history": ([{"d": body.start[:10] if body.start else "", "r": body.rank}] if body.rank is not None else []),
                         "created": _kst_now()})
            _STATE["slots"].insert(0, slot)
            created.append(slot)
        _STATE["logs"].insert(0, {"no": len(_STATE["logs"]) + 1, "kind": "new", "agency": "sm6974", "owner": body.owner, "qty": qty, "days": days,
                                  "created": _kst_now(), "start": body.start, "desc": f"신규 등록 · {kw} · {body.name or (('스마트스토어 상품 ' + body.pid) if body.pid else ('가격비교 상품 ' + body.nvMid))}" + (f" × {qty}" if qty > 1 else ""),
                                  "slot": created[-1]["no"], "user": True})
        _STATE["version"] += 1
        _save_state()
        return {"version": _STATE["version"], "created": created}


def _apply_result_to_slots(job: dict) -> None:
    """워커 결과를 같은 jobId 의 공용 슬롯에 반영."""
    r = job.get("result") or {}
    with _STATE_LOCK:
        _load_state()
        changed = False
        for s in _STATE["slots"]:
            if s.get("jobId") != job["id"]:
                continue
            changed = True
            if job["status"] == "done" and r:
                found = bool(r.get("found"))
                s.update({"name": r.get("name") or s.get("name"), "pid": r.get("pid") or s.get("pid") or "", "nvMid": r.get("nvMid") or s.get("nvMid") or "-",
                          "mall": r.get("mall") or "-", "catalog": bool(r.get("catalog")), "price": r.get("price"), "review": r.get("review"),
                          "url": r.get("url") or s.get("url"), "status": "ok" if found else "err", "rank": r.get("rank") if found else None,
                          "history": ([{"d": (r.get("collectedAt") or "")[:10], "r": r.get("rank")}] if found else []),
                          "note": "" if found else f"\"{s.get('kw')}\" 검색 결과 {int(r.get('rangeMax') or 1000):,}위({r.get('searchedPages')}페이지) 안에 없음",
                          "live": False})
            else:
                s.update({"status": "wait", "live": False, "note": f"즉시 조회 실패: {job.get('error') or '알 수 없음'}",
                          "name": s.get("name") if s.get("name") and not str(s.get("name")).startswith("조회 중") else (f"스마트스토어 상품 {s.get('pid')} (수집 대기)" if s.get("pid") else f"가격비교 상품 {s.get('nvMid')} (수집 대기)"),
                          "mall": "다음 수집 시 자동 조회"})
        if changed:
            _STATE["version"] += 1
            _save_state()


@router.post("/reset")
def reset_state(key: Optional[str] = None):
    """공용 등록 목록 초기화(워커 토큰 필요)."""
    _check_worker(key)
    with _STATE_LOCK:
        _load_state()
        _STATE["slots"] = []
        _STATE["logs"] = []
        _STATE["hidden"] = []
        _STATE["version"] += 1
        _save_state()
    return {"ok": True}


class DeleteIn(BaseModel):
    nos: list[int]


@router.post("/slots/delete")
def delete_slots(body: DeleteIn, request: Request):
    """선택 삭제. 등록 슬롯은 제거, 기본 데모 슬롯(281xxx)은 숨김 목록에 넣어 모든 화면에서 사라지게 한다."""
    if not _rate_ok(_ip(request)):
        raise HTTPException(status_code=429, detail="잠시 후 다시 시도해 주세요")
    nos = set(int(n) for n in body.nos[:500])
    if not nos:
        return {"ok": True, "deleted": 0}
    with _STATE_LOCK:
        _load_state()
        before = len(_STATE["slots"])
        _STATE["slots"] = [x for x in _STATE["slots"] if int(x.get("no") or 0) not in nos]
        removed = before - len(_STATE["slots"])
        user_nos = {int(x.get("no") or 0) for x in _STATE["slots"]}
        hidden = set(int(h) for h in (_STATE.get("hidden") or []))
        hidden |= {n for n in nos if n not in user_nos}
        _STATE["hidden"] = sorted(hidden)
        _STATE["version"] += 1
        _save_state()
        return {"ok": True, "deleted": removed, "hidden": len(_STATE["hidden"]), "version": _STATE["version"]}
