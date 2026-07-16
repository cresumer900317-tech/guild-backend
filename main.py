from contextlib import asynccontextmanager
from typing import Optional, Literal
import json
import os
import time as _time
import unicodedata
import secrets as _secrets
import jwt
from pydantic import BaseModel, field_validator, Field
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import bcrypt
from fastapi.responses import HTMLResponse
from database import supabase
from scheduler import start_scheduler
from schedule_logic import KST, build_schedule
from push_send import _send  # Expo Push 발송 헬퍼(가입 문의 → 운영진 알림)
from static_pages import PRIVACY_HTML, SUPPORT_HTML, TERMS_HTML, DELETE_ACCOUNT_HTML
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

# JWT_SECRET 미설정 시 공개 소스의 기본값으로 토큰이 위조되는 걸 막기 위해
# 부팅마다 랜덤 시크릿 사용(fail-closed). 단 이 경우 재배포 때마다 전원 재로그인 필요 →
# 운영에선 반드시 Railway 환경변수 JWT_SECRET 에 고정 강한 값을 설정할 것.
JWT_SECRET = os.environ.get("JWT_SECRET") or _secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24


# ── 응답 캐시 ─────────────────────────────────────────────────────
# server_ranking 등 무거운 읽기는 데이터가 크롤(수~12시간)때만 바뀌므로 메모리 TTL 캐시로 반복 DB 읽기 제거.
_resp_cache = {}   # key -> (timestamp, value)

def cache_get(key, ttl):
    e = _resp_cache.get(key)
    if e and (_time.time() - e[0]) < ttl:
        return e[1]
    return None

def cache_set(key, value):
    _resp_cache[key] = (_time.time(), value)
    return value

def cache_clear(*keys):
    """키가 '*'로 끝나면 접두사 일치 전체 삭제 (예: 'guild_health_*')."""
    for k in keys:
        if k.endswith("*"):
            prefix = k[:-1]
            for ck in [c for c in _resp_cache if c.startswith(prefix)]:
                _resp_cache.pop(ck, None)
        else:
            _resp_cache.pop(k, None)

def _nfc(s):
    return unicodedata.normalize("NFC", str(s or "")).strip()

POP_ACTIVE = 50   # 길드 건강도 '활동' 기준: 인기도 ≥50 멤버를 활동 멤버로 집계


_SR_MAX = 7000   # 캐시는 항상 전체를 담는다(요청 limit 무관) — limit별 캐시 분기 시 truncate 버그 방지

def load_server_ranking_rows(limit: int = _SR_MAX):
    """server_ranking 전체를 읽어 캐시(10분) 후 limit만큼 slice. server-ranking·guild-health 공유.
    ⚠️ 캐시 키는 limit 무관(항상 전체 저장) — limit=100 호출이 전체 캐시를 망치지 않도록."""
    cached = cache_get("server_ranking_rows", 600)
    if cached is None:
        out, step, start = [], 1000, 0
        while start < _SR_MAX:
            end = min(start + step, _SR_MAX) - 1
            res = supabase.table("server_ranking").select("*").order("server_rank").range(start, end).execute()
            batch = res.data or []
            out.extend(batch)
            if len(batch) < (end - start + 1):
                break
            start += step
        cached = cache_set("server_ranking_rows", out)
    return cached[:limit] if limit else cached


def create_access_token(character_name: str, role: str) -> str:
    payload = {
        "sub": character_name,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_bearer(authorization: Optional[str]) -> dict:
    """Authorization: Bearer <token> 헤더 검증·디코드 (role 구분 없이 공용)"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {"character_name": payload["sub"], "role": payload.get("role", "member")}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="토큰이 만료됐습니다. 다시 로그인해주세요")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다")


def get_current_user(authorization: str = Header(None)) -> dict:
    """길드 API용 유저 추출 — ICP 브릿지 토큰(role=icp)은 길드 API에 못 들어옴 (양방향 분리)"""
    user = _decode_bearer(authorization)
    if user.get("role") == "icp":
        raise HTTPException(status_code=403, detail="ICP 토큰으로는 접근할 수 없습니다")
    return user


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """어드민 권한 체크"""
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
    return current_user


def get_optional_user(authorization: str = Header(None)) -> Optional[dict]:
    """인증 선택적 — 토큰 있으면 유저, 없거나 무효면 None(예외 안 던짐). 좋아요/상세에 사용."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(authorization.split(" ", 1)[1], JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("role") == "icp":   # ICP 토큰은 길드에선 비로그인 취급
            return None
        return {"character_name": payload["sub"], "role": payload.get("role", "member")}
    except Exception:
        return None


def _did_like(board: str, post_id: int, name: Optional[str]) -> bool:
    """해당 유저가 이 글에 좋아요 했는지."""
    if not name:
        return False
    r = (supabase.table("post_likes").select("id")
         .eq("board", board).eq("post_id", post_id).eq("character_name", name)
         .limit(1).execute().data)
    return bool(r)


def _toggle_like(board: str, table: str, post_id: int, user: Optional[dict]) -> dict:
    """좋아요 토글. 로그인 유저면 1인1좋아요 토글(post_likes 기록), 비로그인(웹 하위호환)이면 단순 +1.
    반환: {likes, liked}."""
    row = supabase.table(table).select("likes").eq("id", post_id).execute().data
    if not row:
        raise HTTPException(status_code=404, detail="없는 게시글")
    current = row[0]["likes"] or 0

    if not user:  # 비로그인(웹 등) — 기존 동작 유지
        supabase.table(table).update({"likes": current + 1}).eq("id", post_id).execute()
        return {"likes": current + 1, "liked": False}

    name = user["character_name"]
    existing = (supabase.table("post_likes").select("id")
                .eq("board", board).eq("post_id", post_id).eq("character_name", name)
                .limit(1).execute().data)
    if existing:  # 이미 누름 → 취소
        supabase.table("post_likes").delete().eq("board", board).eq("post_id", post_id)            .eq("character_name", name).execute()
        liked = False
    else:  # 새 좋아요
        supabase.table("post_likes").insert(
            {"board": board, "post_id": post_id, "character_name": name}).execute()
        liked = True
    # likes = 실제 누른 사람 수로 재계산 → ±1 누적 드리프트 불가, 기존 어긋난 값도 자동 교정
    cnt = (supabase.table("post_likes").select("id", count="exact")
           .eq("board", board).eq("post_id", post_id).execute())
    new = cnt.count or 0
    supabase.table(table).update({"likes": new}).eq("id", post_id).execute()
    if liked:
        _notify_post_liked(table, board, post_id, name)
    return {"likes": new, "liked": liked}


def _notify_post_liked(table: str, btype: str, post_id: int, liker: str):
    """좋아요 시 글쓴이에게 푸시(본인 좋아요·취소는 제외, 실패해도 토글은 성공시킨다)."""
    try:
        post = supabase.table(table).select("author,title").eq("id", post_id).execute()
        if not post.data:
            return
        author = post.data[0].get("author")
        title = post.data[0].get("title") or "내 글"
        if not author or author == liker:
            return
        rows = (supabase.table("push_tokens").select("token")
                .eq("character_name", author).execute().data) or []
        tokens = [r["token"] for r in rows]
        if tokens:
            _send(tokens, "\u2764\ufe0f \uc88b\uc544\uc694",
                  f'{liker}\ub2d8\uc774 "{title[:30]}" \uae00\uc744 \uc88b\uc544\ud574\uc694!',
                  {"type": btype, "id": post_id})
    except Exception as e:
        print(f"[\uc88b\uc544\uc694\uc54c\ub9bc] \ud478\uc2dc \uc2e4\ud328: {e}")


# ── Pydantic 요청 모델 ────────────────────────────────────────

class AuthRequest(BaseModel):
    character_name: str
    password: str
    email: Optional[str] = None
    birthdate: Optional[str] = None  # YYYY-MM-DD

    @field_validator("character_name", "password", mode="before")
    @classmethod
    def strip_str(cls, v):
        return (v or "").strip()

class ApproveRequest(BaseModel):
    character_name: str
    guild: Optional[str] = None

class CharacterRequest(BaseModel):
    character_name: str

class RoleChangeRequest(BaseModel):
    character_name: str
    role: Literal["member", "admin", "superadmin"]

class NoticeCreate(BaseModel):
    title: str
    content: str
    category: str = "공지"
    author: str = "운영진"
    author_guild: str = ""
    is_pinned: bool = False

class TipCreate(BaseModel):
    title: str
    content: str
    category: str = "일반"
    author: str = ""
    author_guild: str = ""

class MacroCommentCreate(BaseModel):
    content: str
    author: str = ""
    author_guild: str = ""

class TipCommentCreate(BaseModel):
    content: str
    author: str = ""
    author_guild: str = ""
    parent_id: int | None = None

class TipCommentUpdate(BaseModel):
    content: str

class VisitorPing(BaseModel):
    session_id: str
    character_name: str = "guest"


def to_camel(members):
    result = []
    for m in members:
        result.append({
            "capturedAt": m.get("captured_at"),
            "guild": m.get("guild"),
            "guildLevel": m.get("guild_level", 0),
            "name": m.get("name"),
            "job": m.get("job"),
            "level": m.get("level"),
            "power": m.get("power"),
            "powerText": m.get("power_text"),
            "guildRank": m.get("guild_rank"),
            "overallRank": m.get("overall_rank"),
            "serverRank": m.get("server_rank"),
            "serverRankPrev": m.get("server_rank_prev"),
            "serverRankDiff": m.get("server_rank_diff"),
            "serverRankDirection": m.get("server_rank_direction"),
            "weeklyDiff": m.get("weekly_diff"),
            "growthRate": m.get("growth_rate"),
            "popularity": m.get("popularity"),
            "popServerRank": m.get("pop_server_rank"),
            "bossScore": m.get("boss_score"),
            "bossRank": m.get("boss_rank"),
            "wbossScore": m.get("wboss_score"),
            "wbossRank": m.get("wboss_rank"),
            "detailUrl": m.get("detail_url"),
            "isMaster": m.get("is_master", False),
            "rank": m.get("rank"),
        })
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = start_scheduler()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # 친구들.com(=xn--2e0br5l24w.com), jisoar.com(개인 브랜드·/hq 개인업무앱 이전), *.github.io, localhost 만 허용.
    allow_origin_regex=r"^https://(www\.)?xn--2e0br5l24w\.com$|^https://(www\.)?jisoar\.com$|^https://cresumer900317-tech\.github\.io$|^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# 결혼식 사진 업로드/갤러리
from wedding import router as wedding_router, ensure_table as ensure_wedding_table  # noqa: E402
app.include_router(wedding_router)
try:
    ensure_wedding_table()
except Exception as _e:
    print(f"[wedding] ensure_table skipped: {_e}")


@app.get("/")
def root():
    return {"status": "ok", "message": "친구패밀리 백엔드 작동 중!", "version": "2026-04-15-v3"}


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    """앱스토어 제출용 개인정보처리방침."""
    return HTMLResponse(content=PRIVACY_HTML)


@app.get("/support", response_class=HTMLResponse)
def support_page():
    """앱스토어 제출용 지원 페이지."""
    return HTMLResponse(content=SUPPORT_HTML)


@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    """이용약관 (UGC 무관용 조항 포함)."""
    return HTMLResponse(content=TERMS_HTML)


@app.get("/delete-account", response_class=HTMLResponse)
def delete_account_page():
    """Google Play 데이터 보안용 계정·데이터 삭제 안내 페이지."""
    return HTMLResponse(content=DELETE_ACCOUNT_HTML)


@app.get("/healthz")
def healthz():
    return {"ok": True}


def fetch_members_raw(filters: str = "", order: str = "server_rank"):
    """Supabase REST API로 members 직접 조회 (스키마 캐시 우회)"""
    import httpx
    sb_url = os.environ.get("SUPABASE_URL", "").strip()
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    url = f"{sb_url}/rest/v1/members?select=*&order={order}{filters}"
    resp = httpx.get(url, headers={
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
    }, timeout=15)
    return resp.json()


@app.get("/api/ranking")
def get_ranking():
    data = fetch_members_raw(order="server_rank")
    return to_camel(data)


@app.get("/api/members")
def get_members():
    data = fetch_members_raw()
    return to_camel(data)


@app.post("/api/update-pop-rank")
def update_pop_rank(admin: dict = Depends(require_admin)):
    """
    mgf.gg 스카니아11 인기도 랭킹 페이지를 크롤링해서
    members 테이블의 pop_server_rank 컬럼을 업데이트합니다.
    """
    from fetch_mgf import fetch_popularity_rank
    try:
        # 현재 DB에 있는 멤버 닉네임 목록 가져오기
        result = supabase.table("members").select("id, name").execute()
        members = result.data or []
        if not members:
            return {"status": "ok", "updated": 0, "message": "멤버 없음"}

        name_to_id = {m["name"]: m["id"] for m in members}
        member_names = set(name_to_id.keys())

        # 인기도 랭킹 크롤링
        rank_map = fetch_popularity_rank(member_names)

        # DB 업데이트: REST API로 직접 pop_server_rank 갱신
        import httpx, urllib.parse
        sb_url = os.environ.get("SUPABASE_URL", "").strip()
        sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        headers = {
            "apikey": sb_key,
            "Authorization": f"Bearer {sb_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        updated = 0
        with httpx.Client(timeout=10) as client:
            for name, pop_rank in rank_map.items():
                if name in member_names:
                    url = f"{sb_url}/rest/v1/members?name=eq.{urllib.parse.quote(name)}"
                    client.patch(url, headers=headers, json={"pop_server_rank": pop_rank})
                    updated += 1

            # 랭킹 미발견 멤버는 pop_server_rank = null 로 초기화
            not_found = member_names - set(rank_map.keys())
            for name in not_found:
                url = f"{sb_url}/rest/v1/members?name=eq.{urllib.parse.quote(name)}"
                client.patch(url, headers=headers, json={"pop_server_rank": None})

        return {
            "status": "ok",
            "updated": updated,
            "not_found": len(not_found),
            "message": f"{updated}명 인기도 순위 갱신 완료"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/weekly")
def get_weekly():
    """주간 성장 랭킹 — server_ranking_history의 ~7일 전 스냅샷 대비 멤버별 전투력 성장.
    (기존 members.weekly_diff 정렬은 크롤이 채우지 않는 죽은 필드라 실데이터로 교체.
    guild-health 성장축과 같은 기준일 로직. 이력 없으면 diff=0·hasWeeklyBase=false.)"""
    cached = cache_get("weekly_growth", 600)
    if cached is not None:
        return cached
    members = supabase.table("members").select("*").execute().data or []

    base_date, old_rows = None, {}
    try:
        target = (datetime.now(_KST).date() - timedelta(days=6)).isoformat()
        base = (supabase.table("server_ranking_history").select("snapshot_date")
                .lte("snapshot_date", target).order("snapshot_date", desc=True)
                .limit(1).execute())
        if base.data:
            base_date = base.data[0]["snapshot_date"]
            guilds = sorted({_nfc(m.get("guild")) for m in members if m.get("guild")})
            res = (supabase.table("server_ranking_history").select("name,power,server_rank")
                   .eq("snapshot_date", base_date).in_("guild", guilds).execute())
            for r in res.data or []:
                old_rows[_nfc(r.get("name"))] = r
    except Exception as e:
        print(f"[weekly] 성장 이력 조회 스킵: {repr(e)[:120]}")

    out = []
    for m in members:
        row = dict(m)
        p_now = int(m.get("power") or 0)
        base_row = old_rows.get(_nfc(m.get("name")))
        p_old = int(base_row.get("power") or 0) if base_row else 0
        has_base = bool(p_now and p_old)
        row["weekly_diff"] = (p_now - p_old) if has_base else 0
        row["weekly_growth_rate"] = round((p_now / p_old - 1.0) * 100, 2) if has_base else None
        row["weekly_base_power"] = p_old or None
        row["weekly_base_date"] = base_date if has_base else None
        row["has_weekly_base"] = has_base
        out.append(row)
    out.sort(key=lambda r: r["weekly_diff"], reverse=True)
    payload = to_camel(out)
    for row, src in zip(payload, out):  # to_camel은 고정 화이트리스트라 주간 부가필드는 직접 병합
        row["weeklyGrowthRate"] = src["weekly_growth_rate"]
        row["weeklyBasePower"] = src["weekly_base_power"]
        row["weeklyBaseDate"] = src["weekly_base_date"]
        row["hasWeeklyBase"] = src["has_weekly_base"]
    return cache_set("weekly_growth", payload)


@app.post("/api/snapshot-pop-backfill")
def snapshot_pop_backfill(admin: dict = Depends(require_admin)):
    """이번 달 스냅샷에 현재 인기도 데이터 채우기 (일회용)"""
    now = datetime.now()
    snapshot_month = now.strftime("%Y-%m")

    members = supabase.table("members").select("name,popularity,pop_server_rank").execute()
    updated = 0
    for m in members.data or []:
        supabase.table("monthly_snapshots").update({
            "popularity": m.get("popularity"),
            "pop_server_rank": m.get("pop_server_rank"),
        }).eq("snapshot_month", snapshot_month).eq("name", m["name"]).execute()
        updated += 1
    return {"status": "ok", "message": f"{snapshot_month} 스냅샷에 인기도 {updated}명 반영 완료"}


@app.get("/api/monthly")
def get_monthly():
    """
    이번 달 월간 성장량 계산.
    monthly_snapshots에서 이번 달 스냅샷(월초 전투력)을 가져와
    현재 members 테이블과 비교해 성장량을 계산한다.
    """
    now = datetime.now(_KST)  # Railway=UTC라 KST 명시 (월 경계 9시간 어긋남 방지)
    snapshot_month = now.strftime("%Y-%m")

    # 현재 멤버 데이터
    current_data = fetch_members_raw()
    current_members = {m["name"]: m for m in current_data}

    # 이번 달 월초 스냅샷
    import httpx as _httpx
    _sb_url = os.environ.get("SUPABASE_URL", "").strip()
    _sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    _snap_resp = _httpx.get(
        f"{_sb_url}/rest/v1/monthly_snapshots?select=*&snapshot_month=eq.{snapshot_month}",
        headers={"apikey": _sb_key, "Authorization": f"Bearer {_sb_key}"},
        timeout=15,
    )
    snapshot_map = {s["name"]: s for s in _snap_resp.json()}

    result = []
    for name, cur in current_members.items():
        snap = snapshot_map.get(name)
        cur_power = cur.get("power") or 0
        snap_power = snap.get("power") or 0 if snap else 0

        monthly_diff = cur_power - snap_power if snap else None
        growth_rate = round((monthly_diff / snap_power) * 100, 2) if snap and snap_power > 0 else None

        # 월간 서버 순위 변동 (월초 순위 - 현재 순위, 낮을수록 좋으니 반대로)
        snap_server_rank = snap.get("server_rank") if snap else None
        cur_server_rank = cur.get("server_rank")
        if snap_server_rank and cur_server_rank:
            monthly_server_diff = snap_server_rank - cur_server_rank  # 양수 = 순위 상승
        else:
            monthly_server_diff = None

        # 인기도 성장
        cur_pop = cur.get("popularity") or 0
        snap_pop = snap.get("popularity") or 0 if snap else 0
        pop_diff = cur_pop - snap_pop if snap and snap_pop is not None else None

        snap_pop_rank = snap.get("pop_server_rank") if snap else None
        cur_pop_rank = cur.get("pop_server_rank")
        if snap_pop_rank and cur_pop_rank:
            monthly_pop_rank_diff = snap_pop_rank - cur_pop_rank
        else:
            monthly_pop_rank_diff = None

        result.append({
            "capturedAt": cur.get("captured_at"),
            "guild": cur.get("guild"),
            "guildLevel": cur.get("guild_level", 0),
            "name": name,
            "job": cur.get("job"),
            "level": cur.get("level"),
            "power": cur_power,
            "powerText": cur.get("power_text"),
            "guildRank": cur.get("guild_rank"),
            "overallRank": cur.get("overall_rank"),
            "serverRank": cur.get("server_rank"),
            "serverRankPrev": cur.get("server_rank_prev"),
            "serverRankDiff": cur.get("server_rank_diff"),
            "serverRankDirection": cur.get("server_rank_direction"),
            "monthlyDiff": monthly_diff,
            "growthRate": growth_rate,
            "snapshotMonth": snapshot_month,
            "monthlyServerDiff": monthly_server_diff,
            "hasSnapshot": snap is not None,
            "isMaster": cur.get("is_master", False),
            "detailUrl": cur.get("detail_url"),
            "popularity": cur_pop,
            "popDiff": pop_diff,
            "popServerRank": cur_pop_rank,
            "monthlyPopRankDiff": monthly_pop_rank_diff,
        })

    # 성장량 기준 정렬 (null은 뒤로)
    result.sort(key=lambda x: x.get("monthlyDiff") or -999999999, reverse=True)
    return result


@app.get("/api/home-summary")
def get_home_summary():
    cached = cache_get("home_summary", 300)   # 데이터는 크롤(시간단위)때만 변경 → 5분 캐시
    if cached is not None:
        return cached
    members = fetch_members_raw()
    if not members:
        return {
            "guild_name": "친구패밀리",
            "guild_count": 5,
            "member_count": 0,
            "avg_power": 0,
            "avg_server_rank": 0
        }
    guilds = set(m["guild"] for m in members if m.get("guild"))
    avg_power = int(sum(m["power"] or 0 for m in members) / len(members))
    active = [m["server_rank"] for m in members if m.get("server_rank")]
    avg_rank = round(sum(active) / len(active), 1) if active else 0

    # 이번 달 월간 성장 TOP1
    now = datetime.now(_KST)
    snapshot_month = now.strftime("%Y-%m")
    snap_result = supabase.table("monthly_snapshots")\
        .select("name,power")\
        .eq("snapshot_month", snapshot_month)\
        .execute()
    snap_map = {s["name"]: s["power"] or 0 for s in snap_result.data}

    top_growth = None
    if snap_map:
        best = max(members, key=lambda m: (m.get("power") or 0) - snap_map.get(m["name"], m.get("power") or 0), default=None)
        if best:
            diff = (best.get("power") or 0) - snap_map.get(best["name"], best.get("power") or 0)
            top_growth = {"name": best["name"], "diff": diff}

    return cache_set("home_summary", {
        "guild_name": "친구패밀리",
        "guild_count": len(guilds),
        "member_count": len(members),
        "avg_power": avg_power,
        "avg_server_rank": avg_rank,
        "top_monthly_growth": top_growth,
    })


@app.post("/api/crawl")
def manual_crawl(admin: dict = Depends(require_admin)):
    from scheduler import run_crawl
    run_crawl()
    return {"status": "ok", "message": "크롤링 완료"}


@app.post("/api/snapshot")
def manual_snapshot(admin: dict = Depends(require_admin)):
    """수동으로 이번 달 스냅샷 저장 (테스트용)"""
    from scheduler import run_crawl_and_snapshot
    run_crawl_and_snapshot()
    return {"status": "ok", "message": "월간 스냅샷 저장 완료"}


@app.post("/api/update-boss-rank")
def update_boss_rank(admin: dict = Depends(require_admin)):
    """토벌전/월드보스 점수·서버순위 크롤링 → members 갱신 (수동 트리거)"""
    from scheduler import run_boss_rank_update
    run_boss_rank_update()
    return {"status": "ok", "message": "보스 랭킹 갱신 완료"}


@app.post("/api/update-server-ranking")
def update_server_ranking(admin: dict = Depends(require_admin)):
    """스카니아11 서버 전체 Top3000 크롤 → server_ranking 갱신 (수동 트리거). 테이블 생성 후 호출."""
    from scheduler import run_server_top_update
    run_server_top_update()
    return {"status": "ok", "message": "서버 전체 랭킹 갱신 완료"}


@app.post("/api/update-guild-ranks")
def update_guild_ranks(admin: dict = Depends(require_admin)):
    """친구 길드들의 서버 길드순위 크롤링 → guild_server_ranks 갱신 (수동 트리거)"""
    from scheduler import run_guild_rank_update
    run_guild_rank_update()
    return {"status": "ok", "message": "길드 순위 갱신 완료"}


@app.get("/api/guild-ranks")
def get_guild_ranks():
    """친구 길드들의 스카니아11 서버 길드순위 (서버순위 오름차순)"""
    data = supabase.table("guild_server_ranks").select("*").order("server_rank").execute()
    return [{
        "guildName": r.get("guild_name"),
        "serverRank": r.get("server_rank"),
        "guildLevel": r.get("guild_level"),
        "memberCount": r.get("member_count"),
        "totalPower": r.get("total_power"),
        "capturedAt": r.get("captured_at"),
    } for r in (data.data or [])]


@app.get("/api/server-guild-ranking")
def get_server_guild_ranking(limit: int = 30):
    """스카니아11 서버 전체 길드 랭킹 (서버순위 오름차순). 테이블 미생성/데이터 없음 시 빈 배열."""
    try:
        res = (supabase.table("server_guild_ranking").select("*")
               .order("guild_rank").limit(max(1, min(limit, 100))).execute())
        return [{
            "guildRank": r.get("guild_rank"),
            "guildName": r.get("guild_name"),
            "level": r.get("level"),
            "members": r.get("members"),
            "power": r.get("power"),
            "topPower": r.get("top_power"),
            "lowPower": r.get("low_power"),
            "avgMemberPower": r.get("avg_member_power"),
            "capturedAt": r.get("captured_at"),
        } for r in (res.data or [])]
    except Exception as e:
        print(f"[server-guild-ranking] {e}")
        return []


@app.get("/api/guild-health")
def get_guild_health(limit: int = 30):
    """길드 건강도용 경량 집계 — 길드별 멤버 분포(중앙값·유효기여자수·활동비율)를 서버에서 계산.
    프론트가 server-ranking 전체(1MB+)를 받지 않도록 통계만 반환. 캐시 5분."""
    ckey = f"guild_health_{limit}"
    cached = cache_get(ckey, 300)
    if cached is not None:
        return cached
    try:
        guilds = get_server_guild_ranking(limit)
        members = load_server_ranking_rows()
    except Exception as e:
        print(f"[guild-health] {e}")
        return []
    by = {}
    for m in members:
        g = _nfc(m.get("guild"))
        if g:
            by.setdefault(g, []).append(m)

    # 성장축: server_ranking_history에서 ~7일 전(6일 이전 중 최신) 스냅샷을 기준으로
    # 멤버별 주간 전투력 성장률 계산. 이력 부족/테이블 없음이면 growth 필드는 None(프론트가 축 숨김).
    old_power, growth_base_date = {}, None
    try:
        target = (datetime.now(_KST).date() - timedelta(days=6)).isoformat()
        base = (supabase.table("server_ranking_history").select("snapshot_date")
                .lte("snapshot_date", target).order("snapshot_date", desc=True)
                .limit(1).execute())
        if base.data:
            growth_base_date = base.data[0]["snapshot_date"]
            gnames = [_nfc(g.get("guildName")) for g in guilds if g.get("guildName")]
            start, step = 0, 1000
            while True:
                res = (supabase.table("server_ranking_history").select("name,power")
                       .eq("snapshot_date", growth_base_date)
                       .in_("guild", gnames)
                       .range(start, start + step - 1).execute())
                batch = res.data or []
                for r in batch:
                    old_power[_nfc(r.get("name"))] = int(r.get("power") or 0)
                if len(batch) < step:
                    break
                start += step
    except Exception as ge:
        print(f"[guild-health] growth 이력 조회 스킵: {repr(ge)[:120]}")

    out = []
    for g in guilds:
        ms = by.get(_nfc(g.get("guildName")), [])
        powers = sorted(p for p in (int(m.get("power") or 0) for m in ms) if p > 0)
        pops = [int(m.get("popularity") or 0) for m in ms]
        n = len(powers)
        median = (powers[n // 2] if n % 2 else (powers[n // 2 - 1] + powers[n // 2]) / 2) if n else 0
        tot = sum(powers)
        eff = (1.0 / sum((p / tot) ** 2 for p in powers)) if tot else 0
        active_ratio = (sum(1 for p in pops if p >= POP_ACTIVE) / len(pops)) if pops else None
        # 주간 성장: 두 시점 모두 데이터 있는 멤버 중 +1% 이상 성장한 비율 (0성장=미접속 구분)
        grow = []
        for m in ms:
            p_now = int(m.get("power") or 0)
            p_old = old_power.get(_nfc(m.get("nickname")))
            if p_now > 0 and p_old:
                grow.append(p_now / p_old - 1.0)
        growth_ratio = (sum(1 for x in grow if x > 0.01) / len(grow)) if grow else None
        growth_median = sorted(grow)[len(grow) // 2] if grow else None
        out.append({
            **g,
            "memberSampled": n,
            "medianPower": median,
            "effContributors": round(eff, 2),
            "activeRatio": round(active_ratio, 4) if active_ratio is not None else None,
            "growthRatio": round(growth_ratio, 4) if growth_ratio is not None else None,
            "growthMedianPct": round(growth_median * 100, 2) if growth_median is not None else None,
            "growthSampled": len(grow),
            "growthBaseDate": growth_base_date,
        })
    return cache_set(ckey, out)


@app.post("/api/update-server-guild-ranking")
def update_server_guild_ranking(admin: dict = Depends(require_admin)):
    """서버 전체 길드 랭킹 수동 갱신 트리거. 테이블 생성 후 호출."""
    from scheduler import run_server_guild_update
    run_server_guild_update()
    return {"status": "ok", "message": "서버 길드 랭킹 갱신 완료"}


@app.get("/api/server-boss-ranking")
def get_server_boss_ranking(kind: str = "guild_boss", limit: int = 100):
    """스카니아11 서버 전체 보스 랭킹. kind: guild_boss(토벌전)/world_boss(월드보스). 테이블/데이터 없음 시 빈 배열."""
    if kind not in ("guild_boss", "world_boss"):
        kind = "guild_boss"
    try:
        res = (supabase.table("server_boss_ranking").select("*")
               .eq("kind", kind).order("server_rank").limit(max(1, min(limit, 200))).execute())
        return [{
            "serverRank": r.get("server_rank"),
            "nickname": r.get("nickname"),
            "guild": r.get("guild"),
            "score": r.get("score"),
            "scoreText": r.get("score_text"),
            "level": r.get("level"),
            "job": r.get("job"),
        } for r in (res.data or [])]
    except Exception as e:
        print(f"[server-boss-ranking] {e}")
        return []


@app.post("/api/update-server-boss-ranking")
def update_server_boss_ranking(admin: dict = Depends(require_admin)):
    """서버 전체 보스 랭킹(토벌전·월드보스) 수동 갱신 트리거. 테이블 생성 후 호출."""
    from scheduler import run_server_boss_update
    run_server_boss_update()
    return {"status": "ok", "message": "서버 보스 랭킹 갱신 완료"}


@app.get("/api/server-stats")
def get_server_stats():
    """스카니아11 서버 요약 통계 (홈 히어로용). server_ranking 등재 전체 인원."""
    try:
        cnt = supabase.table("server_ranking").select("server_rank", count="exact").limit(1).execute().count or 0
    except Exception as e:
        print(f"[server-stats] {e}")
        cnt = 0
    return {"totalPlayers": cnt}


@app.get("/api/server-ranking")
def get_server_ranking(limit: int = 7000):
    """스카니아11 서버 전체 전투력 랭킹 (인기도 포함). 테이블 미생성 시 빈 배열."""
    try:
        # 무거운 6800행 읽기는 캐시 사용(데이터는 크롤때만 변경). PostgREST 1000행 캡은 로더가 우회.
        out = load_server_ranking_rows(limit)
        return [{
            "serverRank": r.get("server_rank"),
            "nickname": r.get("nickname"),
            "guild": r.get("guild"),
            "power": r.get("power"),
            "powerText": r.get("power_text"),
            "popularity": r.get("popularity"),
            "level": r.get("level"),
            "job": r.get("job"),
        } for r in out]
    except Exception as e:
        print(f"[server-ranking] {e}")
        return []


@app.get("/api/server-ranking/history")
def get_server_ranking_history(name: str, days: int = 90):
    """특정 캐릭터의 일별 서버랭킹 이력 (프로필 성장 그래프용).
    오래된→최신 순으로 최근 `days`일치 반환. 테이블 미생성/데이터 없음 시 빈 배열."""
    import unicodedata
    nm = unicodedata.normalize("NFC", (name or "").strip())
    if not nm:
        return []
    try:
        res = (supabase.table("server_ranking_history")
               .select("snapshot_date,server_rank,power,popularity,guild")
               .eq("name", nm)
               .order("snapshot_date", desc=True)
               .limit(max(1, min(days, 365)))
               .execute())
        rows = list(reversed(res.data or []))   # desc로 받아 최신 N개 → 시간순 정렬
        return [{
            "date": r.get("snapshot_date"),
            "serverRank": r.get("server_rank"),
            "power": r.get("power"),
            "popularity": r.get("popularity"),
            "guild": r.get("guild"),
        } for r in rows]
    except Exception as e:
        print(f"[server-ranking-history] {e}")
        return []


# ══════════ 포인트/출석 시스템 ══════════
# 적립: 매일 출석(+연속 보너스) + 게시판 글 작성. 사용처: 포인트 랭킹.
# 테이블: user_points(character_name PK, guild, total, streak, last_checkin date), point_log(이력)
_KST = timezone(timedelta(hours=9))
POINTS_CHECKIN_BASE = 10        # 출석 기본
POINTS_POST_FREE = 5           # 자유글 작성
POINTS_POST_TIP = 10           # 팁 작성
POINTS_BOARD_DAILY_CAP = 3     # 하루 게시판 적립 횟수 상한(파밍 방지)


def _kst_now():
    return datetime.now(_KST)


def _member_guild(name: str):
    try:
        r = supabase.table("members").select("guild").eq("name", name).limit(1).execute()
        return (r.data or [{}])[0].get("guild")
    except Exception:
        return None


def _award_points(name: str, amount: int, reason: str, guild=None) -> int:
    """user_points.total 증가 + point_log 기록. 새 total 반환."""
    if guild is None:
        guild = _member_guild(name)
    cur = supabase.table("user_points").select("total").eq("character_name", name).limit(1).execute()
    if cur.data:
        new_total = (cur.data[0].get("total") or 0) + amount
        supabase.table("user_points").update(
            {"total": new_total, "guild": guild, "updated_at": _kst_now().isoformat()}
        ).eq("character_name", name).execute()
    else:
        new_total = amount
        supabase.table("user_points").insert(
            {"character_name": name, "guild": guild, "total": amount}
        ).execute()
    supabase.table("point_log").insert({"character_name": name, "amount": amount, "reason": reason}).execute()
    return new_total


def _board_awards_today(name: str) -> int:
    start = _kst_now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    try:
        r = supabase.table("point_log").select("id").eq("character_name", name)\
            .like("reason", "board:%").gte("created_at", start).execute()
        return len(r.data or [])
    except Exception:
        return 0


def award_board_points(name: str, kind: str, guild=None):
    """게시판 글 작성 적립 — 하루 상한까지만. kind: 'free' | 'tip'. 실패해도 글 작성은 막지 않음."""
    if not name:
        return
    try:
        if _board_awards_today(name) >= POINTS_BOARD_DAILY_CAP:
            return
        amt = POINTS_POST_TIP if kind == "tip" else POINTS_POST_FREE
        _award_points(name, amt, f"board:{kind}", guild)
    except Exception as e:
        print(f"[points] board award fail: {e}")


@app.post("/api/points/checkin")
def points_checkin(user: dict = Depends(get_current_user)):
    """하루 1회 출석. 연속 출석 보너스 + 7일마다 마일스톤."""
    name = user["character_name"]
    today = _kst_now().date()
    cur = supabase.table("user_points").select("*").eq("character_name", name).limit(1).execute()
    row = (cur.data or [None])[0]

    last = None
    streak = 0
    if row:
        streak = row.get("streak") or 0
        if row.get("last_checkin"):
            try:
                last = date.fromisoformat(str(row["last_checkin"])[:10])
            except Exception:
                last = None

    if last == today:
        return {"alreadyChecked": True, "awarded": 0,
                "total": row.get("total") or 0, "streak": streak}

    streak = streak + 1 if last == today - timedelta(days=1) else 1
    base = POINTS_CHECKIN_BASE
    streak_bonus = min(streak, 10) * 2          # 연속일수만큼(최대 +20/일)
    milestone = 50 if streak % 7 == 0 else 0    # 7·14·21일… 보너스
    awarded = base + streak_bonus + milestone
    guild = _member_guild(name)

    if row:
        new_total = (row.get("total") or 0) + awarded
        supabase.table("user_points").update({
            "total": new_total, "streak": streak, "last_checkin": today.isoformat(),
            "guild": guild, "updated_at": _kst_now().isoformat(),
        }).eq("character_name", name).execute()
    else:
        new_total = awarded
        supabase.table("user_points").insert({
            "character_name": name, "guild": guild, "total": awarded,
            "streak": streak, "last_checkin": today.isoformat(),
        }).execute()
    supabase.table("point_log").insert({"character_name": name, "amount": awarded, "reason": f"checkin:streak{streak}"}).execute()

    return {"alreadyChecked": False, "awarded": awarded, "total": new_total,
            "streak": streak, "base": base, "streakBonus": streak_bonus, "milestone": milestone}


@app.get("/api/points/me")
def points_me(user: dict = Depends(get_current_user)):
    name = user["character_name"]
    try:
        cur = supabase.table("user_points").select("*").eq("character_name", name).limit(1).execute()
        row = (cur.data or [None])[0]
    except Exception as e:
        print(f"[points] me {e}")
        row = None
    today = _kst_now().date()
    checked = bool(row and row.get("last_checkin") and str(row["last_checkin"])[:10] == today.isoformat())
    return {
        "characterName": name,
        "total": (row or {}).get("total") or 0,
        "streak": (row or {}).get("streak") or 0,
        "lastCheckin": (row or {}).get("last_checkin"),
        "checkedToday": checked,
    }


@app.get("/api/points/ranking")
def points_ranking(limit: int = 100):
    """포인트 랭킹 (누적 총점 내림차순). 테이블 미생성 시 빈 배열."""
    try:
        r = supabase.table("user_points").select("character_name,guild,total,streak")\
            .order("total", desc=True).limit(limit).execute()
        return [{
            "rank": i + 1,
            "characterName": x.get("character_name"),
            "guild": x.get("guild"),
            "total": x.get("total") or 0,
            "streak": x.get("streak") or 0,
        } for i, x in enumerate(r.data or [])]
    except Exception as e:
        print(f"[points] ranking {e}")
        return []


# ══════════ 멤버 1:1 라이벌(일방 지정) ══════════
# 친구패밀리 멤버끼리 라이벌로 등록 → 프론트가 members 데이터로 1:1 비교.
# ⚠️ 기존 /api/rivals(경쟁 길드)와 별개. 테이블 rival_picks(owner, rival_name).
RIVAL_PICK_CAP = 5


@app.get("/api/rival-picks")
def get_rival_picks(user: dict = Depends(get_current_user)):
    """내가 등록한 라이벌 이름 목록."""
    try:
        r = supabase.table("rival_picks").select("rival_name,created_at")\
            .eq("owner", user["character_name"]).order("created_at").execute()
        return [x.get("rival_name") for x in (r.data or [])]
    except Exception as e:
        print(f"[rival-picks] {e}")
        return []


@app.post("/api/rival-picks")
def add_rival_pick(payload: dict, user: dict = Depends(get_current_user)):
    owner = user["character_name"]
    rival = (payload.get("rival_name") or "").strip()
    if not rival:
        raise HTTPException(status_code=400, detail="라이벌을 선택해주세요")
    if rival == owner:
        raise HTTPException(status_code=400, detail="자기 자신은 라이벌로 등록할 수 없어요")
    existing = supabase.table("rival_picks").select("id").eq("owner", owner).execute()
    if len(existing.data or []) >= RIVAL_PICK_CAP:
        raise HTTPException(status_code=400, detail=f"라이벌은 최대 {RIVAL_PICK_CAP}명까지 등록할 수 있어요")
    supabase.table("rival_picks").upsert(
        {"owner": owner, "rival_name": rival}, on_conflict="owner,rival_name"
    ).execute()
    return {"status": "ok"}


@app.delete("/api/rival-picks/{rival_name}")
def del_rival_pick(rival_name: str, user: dict = Depends(get_current_user)):
    supabase.table("rival_picks").delete()\
        .eq("owner", user["character_name"]).eq("rival_name", rival_name).execute()
    return {"status": "ok"}




# ── 회원 API ──────────────────────────────────────────────────

@app.post("/api/auth/register")
def register(req: AuthRequest):
    """회원가입 — 스카니아11 라운지.
    친구 길드원이거나 server_ranking에 등재된 실제 스카니아11 캐릭터면 즉시 가입(라운지 회원).
    (도용 방지: 실존 캐릭만 + 캐릭당 1계정 + 분쟁 시 운영진 회수)"""
    import unicodedata
    character_name = unicodedata.normalize("NFC", (req.character_name or "").strip())
    password = req.password

    if not character_name or not password:
        raise HTTPException(status_code=400, detail="캐릭터명과 비밀번호를 입력해주세요")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다")

    # 1) 친구 길드원인지 확인 → 길드 혜택 부여
    member_result = supabase.table("members").select("name, guild").eq("name", character_name).execute()
    if member_result.data:
        guild = member_result.data[0].get("guild", "") or ""
    else:
        # 2) 친구 길드원이 아니면, 실제 스카니아11 서버 캐릭터인지 확인 → 라운지 회원
        sr = supabase.table("server_ranking").select("nickname, guild").eq("nickname", character_name).limit(1).execute()
        if not sr.data:
            raise HTTPException(status_code=404, detail="스카니아11 서버에서 찾을 수 없는 캐릭터예요. 캐릭터명을 정확히 입력해주세요. (본인 캐릭터만 가입할 수 있어요)")
        guild = sr.data[0].get("guild", "") or ""

    # 이미 가입된 캐릭터인지 확인 (캐릭당 1계정)
    existing = supabase.table("users").select("id, status").eq("character_name", character_name).execute()
    if existing.data:
        status = existing.data[0]["status"]
        if status == "inactive":
            raise HTTPException(status_code=403, detail="비활성화된 계정입니다. 운영진에게 문의해주세요")
        raise HTTPException(status_code=409, detail="이미 가입된 캐릭터입니다. 본인 캐릭터인데 가입한 적 없다면 운영진에게 문의해주세요(도용 의심)")

    # 비밀번호 해시
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    user_data = {
        "character_name": character_name,
        "password_hash": pw_hash,
        "guild": guild,
        "status": "active",   # 즉시 가입 (운영진 승인 불필요)
        "role": "member",
    }
    if req.email and req.email.strip():
        user_data["email"] = req.email.strip()
    if req.birthdate and req.birthdate.strip():
        user_data["birthdate"] = req.birthdate.strip()

    supabase.table("users").insert(user_data).execute()

    # 가입 즉시 로그인 토큰 발급 (바로 이용)
    token = create_access_token(character_name, "member")
    return {
        "status": "ok",
        "message": "가입 완료! 바로 이용할 수 있어요.",
        "token": token,
        "user": {"character_name": character_name, "guild": guild, "role": "member", "status": "active"},
    }


@app.post("/api/auth/login")
def login(req: AuthRequest):
    """로그인"""
    character_name = req.character_name
    password = req.password

    if not character_name or not password:
        raise HTTPException(status_code=400, detail="캐릭터명과 비밀번호를 입력해주세요")

    result = supabase.table("users")        .select("*")        .eq("character_name", character_name)        .execute()

    if not result.data:
        raise HTTPException(status_code=401, detail="캐릭터명 또는 비밀번호가 틀렸습니다")

    user = result.data[0]

    if user["status"] == "pending":
        raise HTTPException(status_code=403, detail="아직 승인 대기 중입니다. 운영진 승인 후 이용 가능합니다")
    if user["status"] == "inactive":
        raise HTTPException(status_code=403, detail="비활성화된 계정입니다. 운영진에게 문의해주세요")

    if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="캐릭터명 또는 비밀번호가 틀렸습니다")

    token = create_access_token(user["character_name"], user["role"])

    return {
        "status": "ok",
        "token": token,
        "user": {
            "character_name": user["character_name"],
            "guild": user["guild"],
            "role": user["role"],
            "status": user["status"],
        }
    }


@app.get("/api/auth/users")
def get_users(status: str = None, admin: dict = Depends(require_admin)):
    """유저 목록 조회 (어드민용)"""
    query = supabase.table("users").select("id,character_name,guild,status,role,created_at,approved_at")
    if status:
        query = query.eq("status", status)
    result = query.order("created_at", desc=True).execute()
    return result.data or []


@app.post("/api/auth/approve")
def approve_user(req: ApproveRequest, admin: dict = Depends(require_admin)):
    """유저 승인 (어드민용)"""
    supabase.table("users").update({
        "status": "active",
        "guild": req.guild,
        "approved_at": datetime.now().isoformat(),
    }).eq("character_name", req.character_name).execute()

    return {"status": "ok", "message": f"{req.character_name} 승인 완료"}


@app.post("/api/auth/deactivate")
def deactivate_user(req: CharacterRequest, admin: dict = Depends(require_admin)):
    """유저 비활성화 (길드 탈퇴 등)"""
    supabase.table("users").update({
        "status": "inactive",
    }).eq("character_name", req.character_name).execute()

    return {"status": "ok", "message": f"{req.character_name} 비활성화 완료"}


class ResetPasswordRequest(BaseModel):
    character_name: str
    new_password: str

@app.post("/api/auth/reset-password")
def reset_password(req: ResetPasswordRequest, admin: dict = Depends(require_admin)):
    """비밀번호 리셋 (관리자용)"""
    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다")
    try:
        pw_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
        supabase.table("users").update({
            "password_hash": pw_hash,
        }).eq("character_name", req.character_name).execute()
        return {"status": "ok", "message": f"{req.character_name} 비밀번호 리셋 완료"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"리셋 실패: {str(e)}")


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

@app.post("/api/auth/change-password")
def change_password(req: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    """본인 비밀번호 변경 (로그인 필요)"""
    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="새 비밀번호는 4자 이상이어야 합니다")

    # 현재 비밀번호 확인
    result = supabase.table("users").select("password_hash").eq(
        "character_name", user["character_name"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다")

    stored_hash = result.data[0]["password_hash"]
    if not bcrypt.checkpw(req.current_password.encode(), stored_hash.encode()):
        raise HTTPException(status_code=400, detail="현재 비밀번호가 일치하지 않습니다")

    # 새 비밀번호 저장
    new_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
    supabase.table("users").update({"password_hash": new_hash}).eq(
        "character_name", user["character_name"]).execute()
    return {"status": "ok", "message": "비밀번호가 변경되었습니다"}

class RecoverPasswordRequest(BaseModel):
    character_name: str
    email: str
    birthdate: str  # YYYY-MM-DD
    new_password: str

@app.post("/api/auth/recover-password")
def recover_password(req: RecoverPasswordRequest):
    """비밀번호 찾기 — 이메일 + 생년월일 확인 후 새 비밀번호 설정"""
    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다")

    result = supabase.table("users").select(
        "character_name, email, birthdate"
    ).eq("character_name", req.character_name.strip()).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="등록되지 않은 캐릭터명입니다")

    user = result.data[0]
    stored_email = (user.get("email") or "").strip().lower()
    stored_birth = (user.get("birthdate") or "").strip()

    if not stored_email or not stored_birth:
        raise HTTPException(status_code=400,
            detail="이메일/생년월일이 등록되지 않은 계정입니다. 관리자에게 문의해주세요")

    if req.email.strip().lower() != stored_email or req.birthdate.strip() != stored_birth:
        raise HTTPException(status_code=400, detail="이메일 또는 생년월일이 일치하지 않습니다")

    pw_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
    supabase.table("users").update({"password_hash": pw_hash}).eq(
        "character_name", req.character_name.strip()).execute()
    return {"status": "ok", "message": "비밀번호가 재설정되었습니다. 새 비밀번호로 로그인해주세요"}


class UpdateProfileRequest(BaseModel):
    email: Optional[str] = None
    birthdate: Optional[str] = None  # YYYY-MM-DD

@app.post("/api/auth/update-profile")
def update_profile(req: UpdateProfileRequest, user: dict = Depends(get_current_user)):
    """회원정보 변경 (이메일, 생년월일)"""
    updates = {}
    if req.email is not None:
        email = req.email.strip()
        if not email:
            raise HTTPException(status_code=400, detail="이메일을 입력해주세요")
        updates["email"] = email
    if req.birthdate is not None:
        birthdate = req.birthdate.strip()
        if not birthdate:
            raise HTTPException(status_code=400, detail="생년월일을 입력해주세요")
        updates["birthdate"] = birthdate

    if not updates:
        raise HTTPException(status_code=400, detail="변경할 항목이 없습니다")

    supabase.table("users").update(updates).eq(
        "character_name", user["character_name"]).execute()
    return {"status": "ok", "message": "회원정보가 변경되었습니다"}


@app.get("/api/auth/profile")
def get_profile(user: dict = Depends(get_current_user)):
    """내 회원정보 조회"""
    result = supabase.table("users").select(
        "character_name, guild, role, email, birthdate"
    ).eq("character_name", user["character_name"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다")
    return result.data[0]


# [보안] /api/auth/init-superadmin 제거됨 — 인증 없이 하드코딩 비번으로 슈퍼어드민을
# 덮어쓸 수 있는 백도어였음. 슈퍼어드민 비번 재설정이 필요하면 관리자 로그인 후
# /api/auth/reset-password(require_admin) 또는 DB에서 직접 처리할 것.

@app.delete("/api/auth/users/{character_name}")
def delete_user(character_name: str, admin: dict = Depends(require_admin)):
    """유저 삭제 (관리자)"""
    supabase.table("users").delete().eq("character_name", character_name).execute()
    return {"status": "ok", "message": f"{character_name} 삭제 완료"}


@app.delete("/api/auth/me")
def delete_my_account(user: dict = Depends(get_current_user)):
    """본인 계정 탈퇴 (App Store 가이드라인 5.1.1 — 인앱 계정 삭제 의무).
    유저 행 + 본인 푸시 토큰 정리. 게시글/좋아요는 보존(작성자명만 남음)."""
    name = user["character_name"]
    supabase.table("push_tokens").delete().eq("character_name", name).execute()
    supabase.table("users").delete().eq("character_name", name).execute()
    return {"status": "ok"}


# ── UGC 신고/차단 (App Store 가이드라인 1.2) ─────────────────────

class ReportBody(BaseModel):
    target_type: str = Field(alias="targetType")  # post | comment | user
    board: Optional[str] = None                     # tip | free
    target_id: Optional[str] = Field(default=None, alias="targetId")
    reason: Optional[str] = None
    model_config = {"populate_by_name": True}


class BlockBody(BaseModel):
    blocked: str


@app.post("/api/reports")
def create_report(body: ReportBody, user: dict = Depends(get_current_user)):
    """부적절 콘텐츠/이용자 신고 접수. 운영진이 reports 테이블에서 검토."""
    supabase.table("reports").insert({
        "reporter": user["character_name"],
        "target_type": body.target_type,
        "board": body.board,
        "target_id": str(body.target_id) if body.target_id is not None else None,
        "reason": body.reason,
        "status": "open",
    }).execute()
    return {"status": "ok"}


@app.post("/api/blocks")
def block_user(body: BlockBody, user: dict = Depends(get_current_user)):
    """사용자 차단 — 차단 대상의 글/댓글이 차단한 사람에게 보이지 않게 됨."""
    name = user["character_name"]
    if body.blocked == name:
        raise HTTPException(status_code=400, detail="자기 자신은 차단할 수 없습니다")
    supabase.table("blocks").upsert(
        {"blocker": name, "blocked": body.blocked}, on_conflict="blocker,blocked").execute()
    return {"status": "ok"}


@app.delete("/api/blocks/{blocked}")
def unblock_user(blocked: str, user: dict = Depends(get_current_user)):
    """차단 해제."""
    supabase.table("blocks").delete()        .eq("blocker", user["character_name"]).eq("blocked", blocked).execute()
    return {"status": "ok"}


@app.get("/api/blocks")
def list_blocks(user: dict = Depends(get_current_user)):
    """내가 차단한 캐릭터명 목록."""
    rows = (supabase.table("blocks").select("blocked")
            .eq("blocker", user["character_name"]).execute().data) or []
    return [r["blocked"] for r in rows]


# ── 공지사항 API ──────────────────────────────────────────────

@app.get("/api/notices")
def get_notices(summary: bool = False):
    # summary=true 면 본문(content) 제외 — 목록 화면용 (tips/free와 동일 패턴)
    ckey = "notices_summary" if summary else "notices"
    cached = cache_get(ckey, 60)
    if cached is not None:
        return cached
    cols = "id,title,author,author_guild,category,is_pinned,created_at" if summary else "*"
    result = supabase.table("notices")        .select(cols)        .order("is_pinned", desc=True)        .order("created_at", desc=True)        .execute()
    return cache_set(ckey, result.data or [])

@app.post("/api/notices")
def create_notice(req: NoticeCreate, admin: dict = Depends(require_admin)):
    title = req.title.strip()
    content = req.content.strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 입력해주세요")
    result = supabase.table("notices").insert({
        "title": title, "content": content, "category": req.category,
        "author": req.author, "author_guild": req.author_guild, "is_pinned": req.is_pinned,
    }).execute()
    cache_clear("notices*")
    return result.data[0] if result.data else {}

@app.delete("/api/notices/{notice_id}")
def delete_notice(notice_id: int, admin: dict = Depends(require_admin)):
    supabase.table("notices").delete().eq("id", notice_id).execute()
    cache_clear("notices*")
    return {"status": "ok"}


# ── 꿀팁 API ──────────────────────────────────────────────────

@app.get("/api/tips")
def get_tips(category: str = None, summary: bool = False):
    # summary=true 면 본문(content) 제외하고 목록용 가벼운 컬럼만 반환 (모바일 앱 목록 속도용).
    cols = "id,title,author,author_guild,likes,views,created_at,category" if summary else "*"
    query = supabase.table("tips").select(cols).order("created_at", desc=True)
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return result.data or []

@app.post("/api/tips")
def create_tip(req: TipCreate, user: dict = Depends(get_current_user)):
    title = req.title.strip()
    content = req.content.strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 입력해주세요")
    author = user["character_name"]  # 작성자는 토큰 기준 (body author 위조 방지)
    result = supabase.table("tips").insert({
        "title": title, "content": content, "category": req.category,
        "author": author, "author_guild": req.author_guild, "likes": 0, "views": 0,
    }).execute()
    award_board_points(author, "tip", req.author_guild)
    return result.data[0] if result.data else {}

@app.get("/api/tips/{tip_id}")
def get_tip(tip_id: int, user: Optional[dict] = Depends(get_optional_user)):
    result = supabase.table("tips").select("*").eq("id", tip_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    post = result.data[0]
    post["liked"] = _did_like("tip", tip_id, user["character_name"] if user else None)
    return post

@app.get("/api/tips/{tip_id}/adjacent")
def get_adjacent_tips(tip_id: int):
    """현재 글의 이전/다음 글 (id, title만 반환)"""
    prev_result = supabase.table("tips").select("id,title").lt("id", tip_id).order("id", desc=True).limit(1).execute()
    next_result = supabase.table("tips").select("id,title").gt("id", tip_id).order("id", desc=False).limit(1).execute()
    return {
        "prev": prev_result.data[0] if prev_result.data else None,
        "next": next_result.data[0] if next_result.data else None,
    }

@app.post("/api/tips/{tip_id}/like")
def like_tip(tip_id: int, user: Optional[dict] = Depends(get_optional_user)):
    return _toggle_like("tip", "tips", tip_id, user)

@app.post("/api/tips/{tip_id}/view")
def view_tip(tip_id: int):
    result = supabase.table("tips").select("views").eq("id", tip_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    current = result.data[0].get("views") or 0
    supabase.table("tips").update({"views": current + 1}).eq("id", tip_id).execute()
    return {"views": current + 1}

@app.delete("/api/tips/{tip_id}")
def delete_tip(tip_id: int, user: dict = Depends(get_current_user)):
    """본인 글이거나 관리자만 삭제 가능"""
    row = supabase.table("tips").select("author").eq("id", tip_id).execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    if row.data[0].get("author") != user["character_name"] and user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="삭제 권한이 없습니다")
    supabase.table("tips").delete().eq("id", tip_id).execute()
    return {"status": "ok"}


# ── 임시 길드원 등록 API ─────────────────────────────────────────

class TempMemberCreate(BaseModel):
    name: str
    guild: str

@app.post("/api/members/temp")
def add_temp_member(req: TempMemberCreate, admin: dict = Depends(require_admin)):
    """관리자가 임시 길드원을 members 테이블에 등록 (회원가입 가능하도록)"""
    name = req.name.strip()
    guild = req.guild.strip()
    if not name or not guild:
        raise HTTPException(status_code=400, detail="캐릭터명과 길드를 입력해주세요")

    # 이미 존재하는지 확인
    existing = supabase.table("members").select("name").eq("name", name).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail=f"{name}은(는) 이미 등록된 길드원입니다")

    supabase.table("members").insert({
        "name": name,
        "guild": guild,
        "power": 0,
        "level": 0,
        "job": "",
        "power_text": "",
        "captured_at": datetime.now().isoformat(),
    }).execute()

    return {"status": "ok", "message": f"{name} 임시 등록 완료 (길드: {guild})"}


# ── 자유게시판 API ──────────────────────────────────────────────

@app.get("/api/free")
def get_free_posts(summary: bool = False):
    # summary=true 면 본문(content) 제외 (모바일 앱 목록 속도용). 기존 웹은 기본값(전체) 그대로.
    cols = "id,title,author,author_guild,likes,views,created_at" if summary else "*"
    result = supabase.table("free_posts").select(cols).order("created_at", desc=True).execute()
    return result.data or []

@app.get("/api/free/{post_id}")
def get_free_post(post_id: int, user: Optional[dict] = Depends(get_optional_user)):
    result = supabase.table("free_posts").select("*").eq("id", post_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    post = result.data[0]
    post["liked"] = _did_like("free", post_id, user["character_name"] if user else None)
    return post

@app.get("/api/free/{post_id}/adjacent")
def get_free_adjacent(post_id: int):
    current = supabase.table("free_posts").select("created_at").eq("id", post_id).execute()
    if not current.data:
        return {"prev": None, "next": None}
    created = current.data[0]["created_at"]
    prev_result = supabase.table("free_posts").select("id,title").lt("created_at", created).order("created_at", desc=True).limit(1).execute()
    next_result = supabase.table("free_posts").select("id,title").gt("created_at", created).order("created_at").limit(1).execute()
    return {
        "prev": prev_result.data[0] if prev_result.data else None,
        "next": next_result.data[0] if next_result.data else None,
    }

@app.post("/api/free/{post_id}/view")
def view_free_post(post_id: int):
    result = supabase.table("free_posts").select("views").eq("id", post_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    current = result.data[0]["views"] or 0
    supabase.table("free_posts").update({"views": current + 1}).eq("id", post_id).execute()
    return {"views": current + 1}

@app.post("/api/free")
def create_free_post(payload: dict, user: dict = Depends(get_current_user)):
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    author = user["character_name"]  # 작성자는 토큰 기준 (body author 위조 방지)
    author_guild = payload.get("author_guild", "")
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 입력해주세요")
    result = supabase.table("free_posts").insert({
        "title": title, "content": content,
        "author": author, "author_guild": author_guild,
        "likes": 0, "views": 0,
    }).execute()
    award_board_points(author, "free", author_guild)
    return result.data[0] if result.data else {}

@app.post("/api/free/{post_id}/like")
def like_free_post(post_id: int, user: Optional[dict] = Depends(get_optional_user)):
    return _toggle_like("free", "free_posts", post_id, user)

@app.delete("/api/free/{post_id}")
def delete_free_post(post_id: int, user: dict = Depends(get_current_user)):
    """본인 글이거나 관리자만 삭제 가능"""
    row = supabase.table("free_posts").select("author").eq("id", post_id).execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    if row.data[0].get("author") != user["character_name"] and user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="삭제 권한이 없습니다")
    supabase.table("free_comments").delete().eq("post_id", post_id).execute()
    supabase.table("free_posts").delete().eq("id", post_id).execute()
    return {"status": "ok"}


@app.post("/api/auth/role")
def change_role(req: RoleChangeRequest, admin: dict = Depends(require_admin)):
    """role 변경 (superadmin 전용)"""
    supabase.table("users").update({"role": req.role})        .eq("character_name", req.character_name).execute()
    return {"status": "ok", "message": f"{req.character_name} → {req.role}"}


# ── 방문자 API ──────────────────────────────────────────────

@app.post("/api/visitors/ping")
def visitor_ping(req: VisitorPing):
    """방문자 핑 (페이지 로드시 호출)"""
    now = datetime.now()  # last_seen은 UTC naive 유지 — stats의 5분 온라인 창과 같은 기준
    today = datetime.now(_KST).strftime("%Y-%m-%d")  # 일별 경계는 KST (Railway=UTC라 어긋나던 버그)

    # visitors 테이블 upsert (session_id 기준)
    existing = supabase.table("visitors")        .select("id, created_at")        .eq("session_id", req.session_id)        .execute()

    is_new = not existing.data

    supabase.table("visitors").upsert({
        "session_id": req.session_id,
        "character_name": req.character_name,
        "last_seen": now.isoformat(),
    }, on_conflict="session_id").execute()

    # 오늘 방문 카운트 (새 세션만)
    if is_new:
        stat = supabase.table("visit_stats")            .select("count")            .eq("date", today)            .execute()
        if stat.data:
            supabase.table("visit_stats")                .update({"count": stat.data[0]["count"] + 1})                .eq("date", today).execute()
        else:
            supabase.table("visit_stats")                .insert({"date": today, "count": 1}).execute()

    return {"status": "ok", "is_new": is_new}


@app.get("/api/visitors/stats")
def get_visitor_stats():
    """방문자 통계"""
    cached = cache_get("visitor_stats", 20)   # 접속자수 20초 staleness 허용(잦은 호출 부하 완화)
    if cached is not None:
        return cached
    now = datetime.now()  # 온라인 5분 창은 UTC naive (ping의 last_seen과 같은 기준)
    today = datetime.now(_KST).strftime("%Y-%m-%d")  # 일별 경계는 KST

    # 오늘 방문자
    today_stat = supabase.table("visit_stats")        .select("count").eq("date", today).execute()
    today_count = today_stat.data[0]["count"] if today_stat.data else 0

    # 전체 방문자
    all_stats = supabase.table("visit_stats").select("count").execute()
    total_count = sum(r["count"] for r in (all_stats.data or []))

    # 현재 접속자 (최근 5분)
    from datetime import timedelta
    five_min_ago = (now.replace(microsecond=0) - timedelta(minutes=5)).isoformat()
    online = supabase.table("visitors")        .select("session_id, character_name")        .gte("last_seen", five_min_ago)        .execute()
    online_list = online.data or []
    online_count = len(online_list)

    return cache_set("visitor_stats", {
        "today": today_count,
        "total": total_count,
        "online": online_count,
        "online_list": [
            {"name": r["character_name"]} for r in online_list
        ],
    })


# ── 매크로 인증 / 다운로드 API ───────────────────────────────

MACRO_DIR = os.path.join(os.path.dirname(__file__), "macro_files")

@app.get("/api/macro/verify")
def verify_macro_token(user: dict = Depends(get_current_user)):
    """매크로 클라이언트용 토큰 검증 — 활성 길드원만 통과"""
    result = supabase.table("users") \
        .select("status, guild") \
        .eq("character_name", user["character_name"]) \
        .execute()
    if not result.data or result.data[0]["status"] != "active":
        raise HTTPException(status_code=403, detail="비활성 계정입니다")
    return {
        "status": "ok",
        "character_name": user["character_name"],
        "guild": result.data[0].get("guild", ""),
        "role": user["role"],
    }

@app.post("/api/macro/login")
def macro_login(req: AuthRequest):
    """매크로 전용 로그인 — 로그인 + 활성 길드원 검증을 한번에"""
    result = supabase.table("users") \
        .select("*") \
        .eq("character_name", req.character_name) \
        .execute()
    if not result.data:
        raise HTTPException(status_code=401, detail="캐릭터명 또는 비밀번호가 틀렸습니다")
    user = result.data[0]
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="승인되지 않았거나 비활성 계정입니다")
    if not bcrypt.checkpw(req.password.encode(), user["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="캐릭터명 또는 비밀번호가 틀렸습니다")
    token = create_access_token(user["character_name"], user["role"])
    return {
        "status": "ok",
        "token": token,
        "character_name": user["character_name"],
        "guild": user.get("guild", ""),
    }

@app.get("/api/macro/download")
def download_macro(user: dict = Depends(get_current_user)):
    """인증된 길드원만 매크로 파일 다운로드 가능"""
    # 활성 유저 확인
    result = supabase.table("users") \
        .select("status") \
        .eq("character_name", user["character_name"]) \
        .execute()
    if not result.data or result.data[0]["status"] != "active":
        raise HTTPException(status_code=403, detail="비활성 계정입니다")

    macro_path = os.path.join(MACRO_DIR, "ZakumMacro.zip")
    if not os.path.isfile(macro_path):
        raise HTTPException(status_code=404, detail="매크로 파일이 아직 업로드되지 않았습니다")
    return FileResponse(
        macro_path,
        media_type="application/zip",
        filename="ZakumMacro.zip",
    )

# ── 매크로 피드백 댓글 ──────────────────────────────────
@app.get("/api/macro/comments")
def get_macro_comments():
    result = supabase.table("macro_comments") \
        .select("*").order("created_at", desc=True).execute()
    return result.data or []

@app.post("/api/macro/comments")
def create_macro_comment(req: MacroCommentCreate, user: dict = Depends(get_current_user)):
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="내용을 입력해주세요")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="500자 이내로 작성해주세요")
    result = supabase.table("macro_comments").insert({
        "content": content,
        "author": user["character_name"],
        "author_guild": req.author_guild,
    }).execute()
    return result.data[0] if result.data else {}

@app.delete("/api/macro/comments/{comment_id}")
def delete_macro_comment(comment_id: int, user: dict = Depends(get_current_user)):
    # 본인 댓글이거나 admin만 삭제 가능
    comment = supabase.table("macro_comments").select("author").eq("id", comment_id).execute()
    if not comment.data:
        raise HTTPException(status_code=404, detail="없는 댓글")
    if comment.data[0]["author"] != user["character_name"] and user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="삭제 권한이 없습니다")
    supabase.table("macro_comments").delete().eq("id", comment_id).execute()
    return {"status": "ok"}

# ── 팁 게시판 댓글 ──────────────────────────────────
def _notify_post_author(table: str, btype: str, post_id: int, commenter: str, comment_text: str):
    """댓글 작성 시 글쓴이에게 푸시(본인 댓글이면 스킵, 실패해도 댓글 작성은 성공시킨다)."""
    try:
        post = supabase.table(table).select("author,title").eq("id", post_id).execute()
        if not post.data:
            return
        author = post.data[0].get("author")
        title = post.data[0].get("title") or "내 글"
        if not author or author == commenter:
            return  # 본인 글에 본인이 단 댓글엔 알림 안 보냄
        rows = (supabase.table("push_tokens").select("token")
                .eq("character_name", author).execute().data) or []
        tokens = [r["token"] for r in rows]
        if not tokens:
            return
        preview = comment_text[:40]
        _send(tokens, "💬 새 댓글",
              f'{commenter}님이 "{title}"에 댓글을 남겼어요: {preview}',
              {"type": btype, "id": post_id})
    except Exception as e:
        print(f"[댓글알림] 푸시 실패: {e}")


@app.get("/api/tips/{tip_id}/comments")
def get_tip_comments(tip_id: int):
    result = supabase.table("tip_comments") \
        .select("*").eq("tip_id", tip_id).order("created_at", desc=False).execute()
    return result.data or []

@app.post("/api/tips/{tip_id}/comments")
def create_tip_comment(tip_id: int, req: TipCommentCreate, user: dict = Depends(get_current_user)):
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="내용을 입력해주세요")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="500자 이내로 작성해주세요")
    row = {
        "tip_id": tip_id,
        "content": content,
        "author": user["character_name"],
        "author_guild": req.author_guild,
    }
    if req.parent_id is not None:
        row["parent_id"] = req.parent_id
    result = supabase.table("tip_comments").insert(row).execute()
    _notify_post_author("tips", "tip", tip_id, user["character_name"], content)
    return result.data[0] if result.data else {}

@app.patch("/api/tips/comments/{comment_id}")
def update_tip_comment(comment_id: int, req: TipCommentUpdate, user: dict = Depends(get_current_user)):
    comment = supabase.table("tip_comments").select("author").eq("id", comment_id).execute()
    if not comment.data:
        raise HTTPException(status_code=404, detail="없는 댓글")
    if comment.data[0]["author"] != user["character_name"]:
        raise HTTPException(status_code=403, detail="본인 댓글만 수정할 수 있습니다")
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="내용을 입력해주세요")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="500자 이내로 작성해주세요")
    result = supabase.table("tip_comments").update({"content": content}).eq("id", comment_id).execute()
    return result.data[0] if result.data else {}

@app.delete("/api/tips/comments/{comment_id}")
def delete_tip_comment(comment_id: int, user: dict = Depends(get_current_user)):
    comment = supabase.table("tip_comments").select("author").eq("id", comment_id).execute()
    if not comment.data:
        raise HTTPException(status_code=404, detail="없는 댓글")
    if comment.data[0]["author"] != user["character_name"] and user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="삭제 권한이 없습니다")
    supabase.table("tip_comments").delete().eq("id", comment_id).execute()
    return {"status": "ok"}

# --- 자유게시판 댓글 (free_comments) — tip_comments와 동일 구조 ---
@app.get("/api/free/{post_id}/comments")
def get_free_comments(post_id: int):
    result = supabase.table("free_comments") \
        .select("*").eq("post_id", post_id).order("created_at", desc=False).execute()
    return result.data or []

@app.post("/api/free/{post_id}/comments")
def create_free_comment(post_id: int, req: TipCommentCreate, user: dict = Depends(get_current_user)):
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="내용을 입력해주세요")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="500자 이내로 작성해주세요")
    row = {
        "post_id": post_id,
        "content": content,
        "author": user["character_name"],
        "author_guild": req.author_guild,
    }
    if req.parent_id is not None:
        row["parent_id"] = req.parent_id
    result = supabase.table("free_comments").insert(row).execute()
    _notify_post_author("free_posts", "free", post_id, user["character_name"], content)
    return result.data[0] if result.data else {}

@app.patch("/api/free/comments/{comment_id}")
def update_free_comment(comment_id: int, req: TipCommentUpdate, user: dict = Depends(get_current_user)):
    comment = supabase.table("free_comments").select("author").eq("id", comment_id).execute()
    if not comment.data:
        raise HTTPException(status_code=404, detail="없는 댓글")
    if comment.data[0]["author"] != user["character_name"]:
        raise HTTPException(status_code=403, detail="본인 댓글만 수정할 수 있습니다")
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="내용을 입력해주세요")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="500자 이내로 작성해주세요")
    result = supabase.table("free_comments").update({"content": content}).eq("id", comment_id).execute()
    return result.data[0] if result.data else {}

@app.delete("/api/free/comments/{comment_id}")
def delete_free_comment(comment_id: int, user: dict = Depends(get_current_user)):
    comment = supabase.table("free_comments").select("author").eq("id", comment_id).execute()
    if not comment.data:
        raise HTTPException(status_code=404, detail="없는 댓글")
    if comment.data[0]["author"] != user["character_name"] and user.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="삭제 권한이 없습니다")
    supabase.table("free_comments").delete().eq("id", comment_id).execute()
    return {"status": "ok"}


# ── 개인 업무 관리 (Personal Task Manager) ─────────────────────
# single-user: 로그인한 본인 데이터만 owner=character_name 으로 조회/수정

ALLOWED_TASK_STATUS = {"todo", "in_progress", "waiting", "done"}
ALLOWED_TASK_PRIORITY = {"high", "medium", "low"}


class PersonalCategoryCreate(BaseModel):
    name: str
    color: Optional[str] = "#6366f1"
    sort_order: Optional[int] = 0


class PersonalCategoryUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None


class PersonalTaskCreate(BaseModel):
    title: str
    category: Optional[str] = None
    project_id: Optional[int] = None
    parent_task_id: Optional[int] = None
    notes: Optional[str] = ""
    status: Optional[str] = "todo"
    priority: Optional[str] = "medium"
    start_date: Optional[str] = None         # 계획 시작 YYYY-MM-DD
    due_date: Optional[str] = None           # 계획 마감 YYYY-MM-DD
    actual_start_date: Optional[str] = None  # 실제 시작
    actual_end_date: Optional[str] = None    # 실제 완료
    tags: Optional[list[str]] = None
    sort_order: Optional[int] = 0


class PersonalTaskUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    project_id: Optional[int] = None
    parent_task_id: Optional[int] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    start_date: Optional[str] = None
    due_date: Optional[str] = None
    actual_start_date: Optional[str] = None
    actual_end_date: Optional[str] = None
    tags: Optional[list[str]] = None
    sort_order: Optional[int] = None


@app.get("/api/me/categories")
def list_personal_categories(user: dict = Depends(get_current_user)):
    result = supabase.table("personal_categories") \
        .select("*") \
        .eq("owner", user["character_name"]) \
        .order("sort_order").order("id").execute()
    return result.data or []


@app.post("/api/me/categories")
def create_personal_category(req: PersonalCategoryCreate, user: dict = Depends(get_current_user)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="카테고리 이름을 입력해주세요")
    if len(name) > 30:
        raise HTTPException(status_code=400, detail="카테고리 이름은 30자 이내로 입력해주세요")
    existing = supabase.table("personal_categories") \
        .select("id").eq("owner", user["character_name"]).eq("name", name).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="이미 존재하는 카테고리입니다")
    result = supabase.table("personal_categories").insert({
        "owner": user["character_name"],
        "name": name,
        "color": req.color or "#6366f1",
        "sort_order": req.sort_order or 0,
    }).execute()
    return result.data[0] if result.data else {}


@app.patch("/api/me/categories/{category_id}")
def update_personal_category(category_id: int, req: PersonalCategoryUpdate, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_categories") \
        .select("*").eq("id", category_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="카테고리를 찾을 수 없습니다")
    old = existing.data[0]
    updates = {}
    new_name = None
    if req.name is not None:
        new_name = req.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="카테고리 이름을 입력해주세요")
        if len(new_name) > 30:
            raise HTTPException(status_code=400, detail="카테고리 이름은 30자 이내로 입력해주세요")
        if new_name != old["name"]:
            dup = supabase.table("personal_categories") \
                .select("id").eq("owner", user["character_name"]).eq("name", new_name).execute()
            if dup.data:
                raise HTTPException(status_code=409, detail="이미 존재하는 카테고리입니다")
        updates["name"] = new_name
    if req.color is not None:
        updates["color"] = req.color
    if req.sort_order is not None:
        updates["sort_order"] = req.sort_order
    if not updates:
        return old
    result = supabase.table("personal_categories").update(updates).eq("id", category_id).execute()
    # 이름이 바뀌면 task 의 category 필드도 같이 갱신
    if new_name and new_name != old["name"]:
        supabase.table("personal_tasks").update({"category": new_name}) \
            .eq("owner", user["character_name"]).eq("category", old["name"]).execute()
    return result.data[0] if result.data else {}


@app.delete("/api/me/categories/{category_id}")
def delete_personal_category(category_id: int, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_categories") \
        .select("name").eq("id", category_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="카테고리를 찾을 수 없습니다")
    name = existing.data[0]["name"]
    supabase.table("personal_categories").delete().eq("id", category_id).execute()
    # 해당 카테고리를 가진 task 는 category=null 로 (데이터 보존)
    supabase.table("personal_tasks").update({"category": None}) \
        .eq("owner", user["character_name"]).eq("category", name).execute()
    return {"status": "ok"}


@app.get("/api/me/tasks")
def list_personal_tasks(
    status: Optional[str] = None,
    category: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    query = supabase.table("personal_tasks") \
        .select("*").eq("owner", user["character_name"])
    if status and status in ALLOWED_TASK_STATUS:
        query = query.eq("status", status)
    if category:
        query = query.eq("category", category)
    result = query.order("sort_order").order("id", desc=True).execute()
    return result.data or []


@app.post("/api/me/tasks")
def create_personal_task(req: PersonalTaskCreate, user: dict = Depends(get_current_user)):
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="할 일 제목을 입력해주세요")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="제목은 200자 이내로 입력해주세요")
    status = req.status or "todo"
    if status not in ALLOWED_TASK_STATUS:
        raise HTTPException(status_code=400, detail="잘못된 상태값입니다")
    priority = req.priority or "medium"
    if priority not in ALLOWED_TASK_PRIORITY:
        raise HTTPException(status_code=400, detail="잘못된 우선순위입니다")
    row = {
        "owner": user["character_name"],
        "title": title,
        "category": req.category,
        "project_id": req.project_id,
        "parent_task_id": req.parent_task_id if req.parent_task_id and req.parent_task_id > 0 else None,
        "notes": req.notes or "",
        "status": status,
        "priority": priority,
        "start_date": req.start_date or None,
        "due_date": req.due_date or None,
        "actual_start_date": req.actual_start_date or None,
        "actual_end_date": req.actual_end_date or None,
        "tags": req.tags or [],
        "sort_order": req.sort_order or 0,
    }
    if status == "done":
        row["completed_at"] = datetime.now().isoformat()
        if not row["actual_end_date"]:
            row["actual_end_date"] = datetime.now().date().isoformat()
    result = supabase.table("personal_tasks").insert(row).execute()
    return result.data[0] if result.data else {}


@app.patch("/api/me/tasks/{task_id}")
def update_personal_task(task_id: int, req: PersonalTaskUpdate, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_tasks") \
        .select("*").eq("id", task_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="할 일을 찾을 수 없습니다")
    old = existing.data[0]
    updates: dict = {}
    if req.title is not None:
        title = req.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="제목을 입력해주세요")
        if len(title) > 200:
            raise HTTPException(status_code=400, detail="제목은 200자 이내로 입력해주세요")
        updates["title"] = title
    if req.category is not None:
        updates["category"] = req.category or None
    if req.project_id is not None:
        # 0 또는 음수 → null 처리
        updates["project_id"] = req.project_id if req.project_id and req.project_id > 0 else None
    if req.notes is not None:
        updates["notes"] = req.notes
    if req.status is not None:
        if req.status not in ALLOWED_TASK_STATUS:
            raise HTTPException(status_code=400, detail="잘못된 상태값입니다")
        updates["status"] = req.status
        today_str = datetime.now().date().isoformat()
        if req.status == "done" and old["status"] != "done":
            updates["completed_at"] = datetime.now().isoformat()
            # 완료 시 실제 완료일 자동 기록 (기존 값/명시 값이 있으면 유지)
            if not old.get("actual_end_date") and req.actual_end_date is None:
                updates["actual_end_date"] = today_str
        elif req.status != "done":
            updates["completed_at"] = None
        # 진행 중 전환 시 실제 시작일 자동 기록
        if req.status == "in_progress" and not old.get("actual_start_date") and req.actual_start_date is None:
            updates["actual_start_date"] = today_str
    if req.priority is not None:
        if req.priority not in ALLOWED_TASK_PRIORITY:
            raise HTTPException(status_code=400, detail="잘못된 우선순위입니다")
        updates["priority"] = req.priority
    if req.due_date is not None:
        updates["due_date"] = req.due_date or None
    if req.start_date is not None:
        updates["start_date"] = req.start_date or None
    if req.actual_start_date is not None:
        updates["actual_start_date"] = req.actual_start_date or None
    if req.actual_end_date is not None:
        updates["actual_end_date"] = req.actual_end_date or None
    if req.parent_task_id is not None:
        updates["parent_task_id"] = req.parent_task_id if req.parent_task_id and req.parent_task_id > 0 else None
    if req.tags is not None:
        updates["tags"] = req.tags
    if req.sort_order is not None:
        updates["sort_order"] = req.sort_order
    if not updates:
        return old
    updates["updated_at"] = datetime.now().isoformat()
    result = supabase.table("personal_tasks").update(updates).eq("id", task_id).execute()
    return result.data[0] if result.data else {}


@app.delete("/api/me/tasks/{task_id}")
def delete_personal_task(task_id: int, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_tasks") \
        .select("id").eq("id", task_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="할 일을 찾을 수 없습니다")
    supabase.table("personal_tasks").delete().eq("id", task_id).execute()
    return {"status": "ok"}


# ── 프로젝트 ──────────────────────────────────────────────────

ALLOWED_PROJECT_STATUS = {"active", "paused", "done", "dropped"}


class PersonalProjectCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    status: Optional[str] = "active"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    progress_pct: Optional[int] = 0
    color: Optional[str] = "#6366f1"
    notes: Optional[str] = ""
    sort_order: Optional[int] = 0


class PersonalProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    progress_pct: Optional[int] = None
    color: Optional[str] = None
    notes: Optional[str] = None
    sort_order: Optional[int] = None


def _project_with_progress(project: dict, tasks: list[dict]) -> dict:
    """프로젝트에 자동 계산된 task_count, done_count, computed_progress 추가"""
    related = [t for t in tasks if t.get("project_id") == project["id"]]
    total = len(related)
    done = sum(1 for t in related if t.get("status") == "done")
    computed = round(done / total * 100) if total else 0
    return {
        **project,
        "task_count": total,
        "done_count": done,
        "computed_progress": computed,
    }


@app.get("/api/me/projects")
def list_personal_projects(user: dict = Depends(get_current_user)):
    proj_result = supabase.table("personal_projects") \
        .select("*").eq("owner", user["character_name"]) \
        .order("sort_order").order("id").execute()
    projects = proj_result.data or []
    if not projects:
        return []
    task_result = supabase.table("personal_tasks") \
        .select("id,project_id,status").eq("owner", user["character_name"]).execute()
    tasks = task_result.data or []
    return [_project_with_progress(p, tasks) for p in projects]


@app.post("/api/me/projects")
def create_personal_project(req: PersonalProjectCreate, user: dict = Depends(get_current_user)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="프로젝트 이름을 입력해주세요")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="프로젝트 이름은 100자 이내로 입력해주세요")
    status = req.status or "active"
    if status not in ALLOWED_PROJECT_STATUS:
        raise HTTPException(status_code=400, detail="잘못된 상태값입니다")
    progress = req.progress_pct if req.progress_pct is not None else 0
    if progress < 0 or progress > 100:
        raise HTTPException(status_code=400, detail="진행률은 0~100 사이여야 합니다")
    row = {
        "owner": user["character_name"],
        "name": name,
        "description": req.description or "",
        "status": status,
        "start_date": req.start_date or None,
        "end_date": req.end_date or None,
        "progress_pct": progress,
        "color": req.color or "#6366f1",
        "notes": req.notes or "",
        "sort_order": req.sort_order or 0,
    }
    result = supabase.table("personal_projects").insert(row).execute()
    created = result.data[0] if result.data else {}
    return _project_with_progress(created, [])


@app.patch("/api/me/projects/{project_id}")
def update_personal_project(project_id: int, req: PersonalProjectUpdate, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_projects") \
        .select("*").eq("id", project_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다")
    updates: dict = {}
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="이름을 입력해주세요")
        if len(name) > 100:
            raise HTTPException(status_code=400, detail="이름은 100자 이내로 입력해주세요")
        updates["name"] = name
    if req.description is not None:
        updates["description"] = req.description
    if req.status is not None:
        if req.status not in ALLOWED_PROJECT_STATUS:
            raise HTTPException(status_code=400, detail="잘못된 상태값입니다")
        updates["status"] = req.status
    if req.start_date is not None:
        updates["start_date"] = req.start_date or None
    if req.end_date is not None:
        updates["end_date"] = req.end_date or None
    if req.progress_pct is not None:
        if req.progress_pct < 0 or req.progress_pct > 100:
            raise HTTPException(status_code=400, detail="진행률은 0~100 사이여야 합니다")
        updates["progress_pct"] = req.progress_pct
    if req.color is not None:
        updates["color"] = req.color
    if req.notes is not None:
        updates["notes"] = req.notes
    if req.sort_order is not None:
        updates["sort_order"] = req.sort_order
    if not updates:
        return _project_with_progress(existing.data[0], [])
    updates["updated_at"] = datetime.now().isoformat()
    result = supabase.table("personal_projects").update(updates).eq("id", project_id).execute()
    updated = result.data[0] if result.data else existing.data[0]
    task_result = supabase.table("personal_tasks") \
        .select("id,project_id,status").eq("owner", user["character_name"]).execute()
    return _project_with_progress(updated, task_result.data or [])


@app.delete("/api/me/projects/{project_id}")
def delete_personal_project(project_id: int, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_projects") \
        .select("id").eq("id", project_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다")
    # task 의 project_id 는 ON DELETE SET NULL 로 자동 해제
    supabase.table("personal_projects").delete().eq("id", project_id).execute()
    return {"status": "ok"}


# ── 코드 전달함 (Snippets) — 맥↔회사 노트북 코드 브릿지 ──────────

ALLOWED_SNIPPET_KIND = {"single", "tb4", "note"}  # note = 코드 아닌 텍스트 메모 (본문은 content 재사용)
SNIPPET_MAX = 200000  # 코드 한 칸 최대 길이 (≈200KB)
NOTE_MAX = 2000000    # 메모(note)는 태그매핑 표 등 큰 자료 대비 넉넉하게 (≈2MB)


class PersonalSnippetCreate(BaseModel):
    title: Optional[str] = ""
    kind: Optional[str] = "single"
    content: Optional[str] = ""      # kind=single 일 때 본문
    html: Optional[str] = ""         # kind=tb4 (ThingsBoard 4파트)
    css: Optional[str] = ""
    js: Optional[str] = ""
    settings: Optional[str] = ""
    sort_order: Optional[int] = 0


class PersonalSnippetUpdate(BaseModel):
    title: Optional[str] = None
    kind: Optional[str] = None
    content: Optional[str] = None
    html: Optional[str] = None
    css: Optional[str] = None
    js: Optional[str] = None
    settings: Optional[str] = None
    sort_order: Optional[int] = None


def _validate_snippet_len(*parts: Optional[str], kind: str = "single"):
    limit = NOTE_MAX if kind == "note" else SNIPPET_MAX
    msg = "메모가 너무 깁니다 (2MB 이내)" if kind == "note" else "코드가 너무 깁니다 (한 칸 200KB 이내)"
    for p in parts:
        if p and len(p) > limit:
            raise HTTPException(status_code=400, detail=msg)


@app.get("/api/me/snippets")
def list_personal_snippets(user: dict = Depends(get_current_user)):
    result = supabase.table("personal_snippets") \
        .select("*").eq("owner", user["character_name"]) \
        .order("sort_order").order("updated_at", desc=True).execute()
    return result.data or []


@app.post("/api/me/snippets")
def create_personal_snippet(req: PersonalSnippetCreate, user: dict = Depends(get_current_user)):
    kind = req.kind or "single"
    if kind not in ALLOWED_SNIPPET_KIND:
        raise HTTPException(status_code=400, detail="잘못된 종류입니다")
    title = (req.title or "").strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="제목은 200자 이내로 입력해주세요")
    _validate_snippet_len(req.content, req.html, req.css, req.js, req.settings)
    row = {
        "owner": user["character_name"],
        "title": title,
        "kind": kind,
        "content": req.content or "",
        "html": req.html or "",
        "css": req.css or "",
        "js": req.js or "",
        "settings": req.settings or "",
        "sort_order": req.sort_order or 0,
    }
    result = supabase.table("personal_snippets").insert(row).execute()
    return result.data[0] if result.data else {}


@app.patch("/api/me/snippets/{snippet_id}")
def update_personal_snippet(snippet_id: int, req: PersonalSnippetUpdate, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_snippets") \
        .select("*").eq("id", snippet_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="스니펫을 찾을 수 없습니다")
    _validate_snippet_len(req.content, req.html, req.css, req.js, req.settings)
    updates: dict = {}
    if req.title is not None:
        t = req.title.strip()
        if len(t) > 200:
            raise HTTPException(status_code=400, detail="제목은 200자 이내로 입력해주세요")
        updates["title"] = t
    if req.kind is not None:
        if req.kind not in ALLOWED_SNIPPET_KIND:
            raise HTTPException(status_code=400, detail="잘못된 종류입니다")
        updates["kind"] = req.kind
    for field in ("content", "html", "css", "js", "settings"):
        val = getattr(req, field)
        if val is not None:
            updates[field] = val
    if req.sort_order is not None:
        updates["sort_order"] = req.sort_order
    if not updates:
        return existing.data[0]
    updates["updated_at"] = datetime.now().isoformat()
    result = supabase.table("personal_snippets").update(updates).eq("id", snippet_id).execute()
    return result.data[0] if result.data else {}


@app.delete("/api/me/snippets/{snippet_id}")
def delete_personal_snippet(snippet_id: int, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_snippets") \
        .select("id").eq("id", snippet_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="스니펫을 찾을 수 없습니다")
    supabase.table("personal_snippets").delete().eq("id", snippet_id).execute()
    return {"status": "ok"}


@app.delete("/api/me/snippets")
def clear_personal_snippets(user: dict = Depends(get_current_user)):
    """전달함 전체 비우기 — 내 스니펫 일괄 삭제."""
    count = supabase.table("personal_snippets") \
        .select("id", count="exact").eq("owner", user["character_name"]).execute().count or 0
    supabase.table("personal_snippets").delete().eq("owner", user["character_name"]).execute()
    return {"status": "ok", "deleted": count}


# ── ICP 코드 브릿지 — jisoar.com/icp (접속코드 인증, 공용 작업공간) ──────────
# 길드 계정과 완전 분리: /api/icp/login 이 발급하는 토큰은 role="icp", sub="icp:<이름>"
# 이라 길드 유저를 사칭할 수 없고, 길드 토큰(role=member)은 ICP 엔드포인트에 못 들어옴.
# 운영: Railway 환경변수 ICP_ACCESS_CODE 에 접속코드 설정 (미설정 시 로그인 차단).
# 테이블: icp_snippets (personal_snippets와 동일 칼럼 + owner 대신 author, 전원 공유)
ICP_ACCESS_CODE = os.environ.get("ICP_ACCESS_CODE", "").strip()

# 개인별 접속코드 — ICP_ACCESS_CODES="Jett:코드,Minhyun:코드" 형식.
# 설정돼 있으면 공용 ICP_ACCESS_CODE 는 무시되고, 본인 이름+본인 코드 조합만 로그인 가능.
ICP_ACCESS_CODES: dict = {}
for _pair in os.environ.get("ICP_ACCESS_CODES", "").split(","):
    if ":" in _pair:
        _n, _c = _pair.split(":", 1)
        if _n.strip() and _c.strip():
            ICP_ACCESS_CODES[_n.strip()] = _c.strip()

# 고정 사용자 — 개인코드가 있으면 그 이름들이 곧 허용목록, 없으면 ICP_MEMBERS 폴백
ICP_MEMBERS = set(ICP_ACCESS_CODES) or \
    {m.strip() for m in os.environ.get("ICP_MEMBERS", "Jett,Minhyun").split(",") if m.strip()}

# 로그인 실패 잠금 — IP당 10분 안에 5회 실패하면 그 IP는 창이 빌 때까지 차단(성공 시 초기화)
ICP_FAIL_MAX = 5
ICP_FAIL_WINDOW = 600  # 초
_icp_fails: dict = {}  # ip -> [실패 시각(epoch), ...]


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _icp_guard_bruteforce(ip: str):
    now = _time.time()
    fails = [t for t in _icp_fails.get(ip, []) if now - t < ICP_FAIL_WINDOW]
    _icp_fails[ip] = fails
    if len(fails) >= ICP_FAIL_MAX:
        raise HTTPException(status_code=429, detail="로그인 시도가 너무 많아요. 10분 뒤에 다시 시도해주세요")


def _icp_record_fail(ip: str):
    _icp_fails.setdefault(ip, []).append(_time.time())
    if len(_icp_fails) > 10000:  # 메모리 방어 — 오래된 IP 엔트리 정리
        cutoff = _time.time() - ICP_FAIL_WINDOW
        for k in [k for k, v in _icp_fails.items() if not v or v[-1] < cutoff]:
            _icp_fails.pop(k, None)


class IcpLoginRequest(BaseModel):
    code: str
    name: str


def get_icp_user(authorization: str = Header(None)) -> dict:
    user = _decode_bearer(authorization)
    if user.get("role") != "icp":
        raise HTTPException(status_code=403, detail="ICP 접근 권한이 없습니다")
    raw = user["character_name"]
    return {"name": raw[4:] if raw.startswith("icp:") else raw, "role": "icp"}


@app.post("/api/icp/login")
def icp_login(req: IcpLoginRequest, request: Request):
    if not ICP_ACCESS_CODES and not ICP_ACCESS_CODE:
        raise HTTPException(status_code=503, detail="접속코드가 아직 설정되지 않았어요 (관리자에게 문의)")
    ip = _client_ip(request)
    _icp_guard_bruteforce(ip)
    name = (req.name or "").strip()
    code = (req.code or "").strip()
    if name not in ICP_MEMBERS:
        _icp_record_fail(ip)
        raise HTTPException(status_code=400, detail="등록된 사용자가 아니에요")
    expected = ICP_ACCESS_CODES.get(name, "") if ICP_ACCESS_CODES else ICP_ACCESS_CODE
    if not expected or not _secrets.compare_digest(code, expected):
        _icp_record_fail(ip)
        raise HTTPException(status_code=401, detail="접속코드가 올바르지 않아요")
    _icp_fails.pop(ip, None)
    return {"token": create_access_token(f"icp:{name}", role="icp"), "name": name}


@app.get("/api/icp/snippets")
def list_icp_snippets(user: dict = Depends(get_icp_user)):
    result = supabase.table("icp_snippets").select("*") \
        .order("sort_order").order("updated_at", desc=True).execute()
    return result.data or []


@app.post("/api/icp/snippets")
def create_icp_snippet(req: PersonalSnippetCreate, user: dict = Depends(get_icp_user)):
    kind = req.kind or "single"
    if kind not in ALLOWED_SNIPPET_KIND:
        raise HTTPException(status_code=400, detail="잘못된 종류입니다")
    title = (req.title or "").strip()
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="제목은 200자 이내로 입력해주세요")
    _validate_snippet_len(req.content, req.html, req.css, req.js, req.settings, kind=kind)
    row = {
        "author": user["name"],
        "title": title,
        "kind": kind,
        "content": req.content or "",
        "html": req.html or "",
        "css": req.css or "",
        "js": req.js or "",
        "settings": req.settings or "",
        "sort_order": req.sort_order or 0,
    }
    result = supabase.table("icp_snippets").insert(row).execute()
    return result.data[0] if result.data else {}


@app.patch("/api/icp/snippets/{snippet_id}")
def update_icp_snippet(snippet_id: int, req: PersonalSnippetUpdate, user: dict = Depends(get_icp_user)):
    existing = supabase.table("icp_snippets").select("*").eq("id", snippet_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="스니펫을 찾을 수 없습니다")
    effective_kind = req.kind or existing.data[0].get("kind") or "single"
    _validate_snippet_len(req.content, req.html, req.css, req.js, req.settings, kind=effective_kind)
    updates: dict = {}
    if req.title is not None:
        t = req.title.strip()
        if len(t) > 200:
            raise HTTPException(status_code=400, detail="제목은 200자 이내로 입력해주세요")
        updates["title"] = t
    if req.kind is not None:
        if req.kind not in ALLOWED_SNIPPET_KIND:
            raise HTTPException(status_code=400, detail="잘못된 종류입니다")
        updates["kind"] = req.kind
    for field in ("content", "html", "css", "js", "settings"):
        val = getattr(req, field)
        if val is not None:
            updates[field] = val
    if req.sort_order is not None:
        updates["sort_order"] = req.sort_order
    if not updates:
        return existing.data[0]
    updates["updated_at"] = datetime.now().isoformat()
    result = supabase.table("icp_snippets").update(updates).eq("id", snippet_id).execute()
    return result.data[0] if result.data else {}


@app.delete("/api/icp/snippets/{snippet_id}")
def delete_icp_snippet(snippet_id: int, user: dict = Depends(get_icp_user)):
    existing = supabase.table("icp_snippets").select("id").eq("id", snippet_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="스니펫을 찾을 수 없습니다")
    supabase.table("icp_snippets").delete().eq("id", snippet_id).execute()
    return {"status": "ok"}


# ── Inbox (즉흥 메모) ─────────────────────────────────────────

class PersonalInboxCreate(BaseModel):
    content: str


class PersonalInboxUpdate(BaseModel):
    content: Optional[str] = None
    processed: Optional[bool] = None


class PersonalInboxPromote(BaseModel):
    """Inbox 항목을 Task 로 승격할 때 사용"""
    title: Optional[str] = None  # 미지정 시 content 그대로
    category: Optional[str] = None
    project_id: Optional[int] = None
    priority: Optional[str] = "medium"
    due_date: Optional[str] = None


@app.get("/api/me/inbox")
def list_personal_inbox(
    processed: Optional[bool] = None,
    limit: int = 100,
    user: dict = Depends(get_current_user),
):
    query = supabase.table("personal_inbox") \
        .select("*").eq("owner", user["character_name"])
    if processed is not None:
        query = query.eq("processed", processed)
    result = query.order("created_at", desc=True).limit(max(1, min(limit, 500))).execute()
    return result.data or []


@app.post("/api/me/inbox")
def create_personal_inbox(req: PersonalInboxCreate, user: dict = Depends(get_current_user)):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="내용을 입력해주세요")
    if len(content) > 2000:
        raise HTTPException(status_code=400, detail="2000자 이내로 입력해주세요")
    row = {
        "owner": user["character_name"],
        "content": content,
        "processed": False,
    }
    result = supabase.table("personal_inbox").insert(row).execute()
    return result.data[0] if result.data else {}


@app.patch("/api/me/inbox/{inbox_id}")
def update_personal_inbox(inbox_id: int, req: PersonalInboxUpdate, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_inbox") \
        .select("*").eq("id", inbox_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Inbox 항목을 찾을 수 없습니다")
    updates: dict = {}
    if req.content is not None:
        content = req.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="내용을 입력해주세요")
        updates["content"] = content
    if req.processed is not None:
        updates["processed"] = req.processed
        updates["processed_at"] = datetime.now().isoformat() if req.processed else None
    if not updates:
        return existing.data[0]
    result = supabase.table("personal_inbox").update(updates).eq("id", inbox_id).execute()
    return result.data[0] if result.data else {}


@app.delete("/api/me/inbox/{inbox_id}")
def delete_personal_inbox(inbox_id: int, user: dict = Depends(get_current_user)):
    existing = supabase.table("personal_inbox") \
        .select("id").eq("id", inbox_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Inbox 항목을 찾을 수 없습니다")
    supabase.table("personal_inbox").delete().eq("id", inbox_id).execute()
    return {"status": "ok"}


@app.post("/api/me/inbox/{inbox_id}/promote")
def promote_inbox_to_task(inbox_id: int, req: PersonalInboxPromote, user: dict = Depends(get_current_user)):
    """Inbox 항목 → 새 Task 로 승격하고 inbox 항목은 processed 처리"""
    existing = supabase.table("personal_inbox") \
        .select("*").eq("id", inbox_id).eq("owner", user["character_name"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Inbox 항목을 찾을 수 없습니다")
    inbox = existing.data[0]

    raw_title = (req.title or inbox["content"] or "").strip()
    if not raw_title:
        raise HTTPException(status_code=400, detail="제목이 비어있습니다")
    title = raw_title[:200]
    notes = "" if req.title else ""  # 제목이 본문 자체면 별도 메모 비움
    if req.title and inbox["content"] and inbox["content"] != req.title:
        notes = inbox["content"]

    priority = req.priority or "medium"
    if priority not in ALLOWED_TASK_PRIORITY:
        priority = "medium"

    task_row = {
        "owner": user["character_name"],
        "title": title,
        "category": req.category,
        "project_id": req.project_id,
        "notes": notes,
        "status": "todo",
        "priority": priority,
        "due_date": req.due_date or None,
        "tags": [],
        "sort_order": 0,
    }
    task_result = supabase.table("personal_tasks").insert(task_row).execute()
    new_task = task_result.data[0] if task_result.data else None

    supabase.table("personal_inbox").update({
        "processed": True,
        "processed_at": datetime.now().isoformat(),
        "promoted_task_id": new_task["id"] if new_task else None,
    }).eq("id", inbox_id).execute()

    return {"task": new_task, "inbox_id": inbox_id}


# ── Daily Logs (그날 뭐 했는지) ──────────────────────────────

class PersonalDailyLogUpsert(BaseModel):
    log_date: Optional[str] = None  # YYYY-MM-DD; 미지정 시 오늘
    content: str


@app.get("/api/me/daily-logs")
def list_personal_daily_logs(
    start: Optional[str] = None,  # YYYY-MM-DD
    end: Optional[str] = None,    # YYYY-MM-DD
    limit: int = 60,
    user: dict = Depends(get_current_user),
):
    query = supabase.table("personal_daily_logs") \
        .select("*").eq("owner", user["character_name"])
    if start:
        query = query.gte("log_date", start)
    if end:
        query = query.lte("log_date", end)
    result = query.order("log_date", desc=True).limit(max(1, min(limit, 366))).execute()
    return result.data or []


@app.get("/api/me/daily-logs/{log_date}")
def get_personal_daily_log(log_date: str, user: dict = Depends(get_current_user)):
    """특정 날짜 로그 조회 — 없으면 빈 객체 반환"""
    result = supabase.table("personal_daily_logs") \
        .select("*").eq("owner", user["character_name"]).eq("log_date", log_date).execute()
    if not result.data:
        return {"log_date": log_date, "content": "", "id": None}
    return result.data[0]


@app.put("/api/me/daily-logs")
def upsert_personal_daily_log(req: PersonalDailyLogUpsert, user: dict = Depends(get_current_user)):
    """날짜 단위 upsert — 같은 날짜에 두 번 저장하면 덮어씀"""
    log_date = req.log_date or datetime.now().strftime("%Y-%m-%d")
    content = req.content if req.content is not None else ""
    if len(content) > 20000:
        raise HTTPException(status_code=400, detail="20000자 이내로 입력해주세요")

    existing = supabase.table("personal_daily_logs") \
        .select("id").eq("owner", user["character_name"]).eq("log_date", log_date).execute()
    if existing.data:
        result = supabase.table("personal_daily_logs").update({
            "content": content,
            "updated_at": datetime.now().isoformat(),
        }).eq("id", existing.data[0]["id"]).execute()
    else:
        result = supabase.table("personal_daily_logs").insert({
            "owner": user["character_name"],
            "log_date": log_date,
            "content": content,
        }).execute()
    return result.data[0] if result.data else {"log_date": log_date, "content": content}


@app.delete("/api/me/daily-logs/{log_date}")
def delete_personal_daily_log(log_date: str, user: dict = Depends(get_current_user)):
    supabase.table("personal_daily_logs") \
        .delete().eq("owner", user["character_name"]).eq("log_date", log_date).execute()
    return {"status": "ok"}


# ── 대시보드 요약 (한 번에 다 가져오기) ──────────────────────

@app.get("/api/me/dashboard")
def get_personal_dashboard(user: dict = Depends(get_current_user)):
    """대시보드 한 번에 — tasks, projects, inbox, recent daily logs 한꺼번에 반환"""
    owner = user["character_name"]
    tasks = supabase.table("personal_tasks") \
        .select("*").eq("owner", owner).execute().data or []
    projects = supabase.table("personal_projects") \
        .select("*").eq("owner", owner).order("sort_order").order("id").execute().data or []
    inbox = supabase.table("personal_inbox") \
        .select("*").eq("owner", owner).eq("processed", False) \
        .order("created_at", desc=True).limit(20).execute().data or []
    logs = supabase.table("personal_daily_logs") \
        .select("*").eq("owner", owner) \
        .order("log_date", desc=True).limit(7).execute().data or []
    categories = supabase.table("personal_categories") \
        .select("*").eq("owner", owner).order("sort_order").execute().data or []

    return {
        "tasks": tasks,
        "projects": [_project_with_progress(p, tasks) for p in projects],
        "inbox": inbox,
        "daily_logs": logs,
        "categories": categories,
    }


# ════════════════════════════════════════════════════════
# AI 정리 (Phase 5) — Claude haiku 호출
# ════════════════════════════════════════════════════════

import ai as ai_service


class PromoteExtractRequest(BaseModel):
    kind: Literal["tasks", "future"]
    index: int
    category: Optional[str] = None
    project_id: Optional[int] = None
    priority: Literal["high", "medium", "low"] = "medium"
    due_date: Optional[str] = None  # YYYY-MM-DD
    title_override: Optional[str] = None


class DismissExtractRequest(BaseModel):
    kind: Literal["tasks", "future", "decisions"]
    index: int


class PersonalSearchRequest(BaseModel):
    query: str
    days: int = 90


def _require_ai():
    if not ai_service.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="AI 기능이 비활성 상태입니다. ANTHROPIC_API_KEY 가 설정되지 않았습니다.",
        )


@app.get("/api/me/ai/status")
def ai_status():
    """프런트가 AI 가능 여부를 확인하기 위한 경량 엔드포인트."""
    return {"enabled": ai_service.is_enabled(), "model": ai_service.CLAUDE_MODEL}


@app.post("/api/me/daily-logs/{log_date}/analyze")
def analyze_daily_log(log_date: str, user: dict = Depends(get_current_user)):
    """하루 로그를 분석해 할 일/미래/결정/태그 추출. 동일 내용은 캐시 재사용."""
    _require_ai()
    owner = user["character_name"]

    log_result = supabase.table("personal_daily_logs") \
        .select("*").eq("owner", owner).eq("log_date", log_date).execute()
    if not log_result.data:
        raise HTTPException(status_code=404, detail="해당 날짜 로그가 없습니다")
    content = (log_result.data[0].get("content") or "").strip()
    if not content:
        return {
            "status": "empty",
            "cached": False,
            "id": None,
            "extract": {"tasks": [], "future": [], "decisions": [], "tags": []},
            "promoted": [],
            "dismissed": [],
        }

    src_hash = ai_service.content_hash(content)

    cache = supabase.table("personal_ai_summaries") \
        .select("*") \
        .eq("owner", owner) \
        .eq("kind", ai_service.EXTRACT_KIND) \
        .eq("source_hash", src_hash) \
        .order("created_at", desc=True).limit(1).execute()
    if cache.data:
        row = cache.data[0]
        payload = row.get("payload") or {}
        return {
            "status": "ok",
            "cached": True,
            "id": row.get("id"),
            "extract": payload.get("extract") or _empty_extract_obj(),
            "promoted": payload.get("promoted", []),
            "dismissed": payload.get("dismissed", []),
        }

    try:
        extract = ai_service.extract_from_daily_log(content, log_date, owner=owner)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 호출 실패: {str(e)[:200]}")

    payload = {
        "extract": extract,
        "log_date": log_date,
        "promoted": [],
        "dismissed": [],
    }
    inserted = supabase.table("personal_ai_summaries").insert({
        "owner": owner,
        "kind": ai_service.EXTRACT_KIND,
        "source_hash": src_hash,
        "payload": payload,
    }).execute()
    row = inserted.data[0] if inserted.data else {}
    return {
        "status": "ok",
        "cached": False,
        "id": row.get("id"),
        "extract": extract,
        "promoted": [],
        "dismissed": [],
    }


def _empty_extract_obj() -> dict:
    return {"tasks": [], "future": [], "decisions": [], "tags": []}


@app.get("/api/me/daily-logs/{log_date}/extracts")
def get_daily_extracts(log_date: str, user: dict = Depends(get_current_user)):
    """해당 날짜 로그의 가장 최근 추출 결과. 없으면 빈 객체."""
    owner = user["character_name"]
    log_result = supabase.table("personal_daily_logs") \
        .select("content").eq("owner", owner).eq("log_date", log_date).execute()
    if not log_result.data or not (log_result.data[0].get("content") or "").strip():
        return {"id": None, "extract": None, "promoted": [], "dismissed": []}
    content = log_result.data[0]["content"]
    src_hash = ai_service.content_hash(content)

    cache = supabase.table("personal_ai_summaries") \
        .select("*") \
        .eq("owner", owner) \
        .eq("kind", ai_service.EXTRACT_KIND) \
        .eq("source_hash", src_hash) \
        .order("created_at", desc=True).limit(1).execute()
    if not cache.data:
        return {"id": None, "extract": None, "promoted": [], "dismissed": []}
    row = cache.data[0]
    payload = row.get("payload") or {}
    return {
        "id": row.get("id"),
        "extract": payload.get("extract"),
        "promoted": payload.get("promoted", []),
        "dismissed": payload.get("dismissed", []),
        "created_at": row.get("created_at"),
    }


@app.post("/api/me/extracts/{eid}/promote")
def promote_extract(eid: int, req: PromoteExtractRequest, user: dict = Depends(get_current_user)):
    """추출된 항목 하나를 personal_tasks 로 승격."""
    owner = user["character_name"]
    row_result = supabase.table("personal_ai_summaries") \
        .select("*").eq("owner", owner).eq("id", eid).execute()
    if not row_result.data:
        raise HTTPException(status_code=404, detail="추출 결과를 찾을 수 없습니다")
    row = row_result.data[0]
    payload = row.get("payload") or {}
    extract = payload.get("extract") or {}
    items = extract.get(req.kind, [])
    if req.index < 0 or req.index >= len(items):
        raise HTTPException(status_code=400, detail="인덱스가 범위를 벗어납니다")
    item = items[req.index]
    override = (req.title_override or "").strip()
    title = override or (item.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="빈 제목입니다")

    log_date_hint = payload.get("log_date") or ""
    task_row = {
        "owner": owner,
        "title": title,
        "category": req.category,
        "project_id": req.project_id,
        "status": "todo",
        "priority": req.priority,
        "due_date": req.due_date,
        "tags": [],
        "notes": f"하루 로그({log_date_hint})에서 AI 가 추출",
    }
    insert_result = supabase.table("personal_tasks").insert(task_row).execute()
    new_task = insert_result.data[0] if insert_result.data else None

    key = f"{req.kind}:{req.index}"
    promoted = list(payload.get("promoted", []) or [])
    if key not in promoted:
        promoted.append(key)
    payload["promoted"] = promoted
    supabase.table("personal_ai_summaries").update({"payload": payload}).eq("id", eid).execute()

    return {"status": "ok", "task": new_task, "promoted": promoted}


@app.post("/api/me/extracts/{eid}/dismiss")
def dismiss_extract(eid: int, req: DismissExtractRequest, user: dict = Depends(get_current_user)):
    owner = user["character_name"]
    row_result = supabase.table("personal_ai_summaries") \
        .select("*").eq("owner", owner).eq("id", eid).execute()
    if not row_result.data:
        raise HTTPException(status_code=404, detail="추출 결과를 찾을 수 없습니다")
    payload = row_result.data[0].get("payload") or {}
    key = f"{req.kind}:{req.index}"
    dismissed = list(payload.get("dismissed", []) or [])
    if key not in dismissed:
        dismissed.append(key)
    payload["dismissed"] = dismissed
    supabase.table("personal_ai_summaries").update({"payload": payload}).eq("id", eid).execute()
    return {"status": "ok", "dismissed": dismissed}


# ── Inbox AI classification (Phase 6d) ────────────────────────

def _classify_one_inbox(inbox_row: dict, categories: list[str], owner: str) -> dict:
    """Run AI classification on a single inbox row, with cache lookup/store.

    Returns dict suitable to embed as `suggestion` next to the inbox item.
    """
    content = (inbox_row.get("content") or "").strip()
    if not content:
        return {"inbox_id": inbox_row.get("id"), "cached": False, **ai_service._empty_classify()}

    src_hash = ai_service.content_hash(content)

    cache = supabase.table("personal_ai_summaries") \
        .select("id, payload") \
        .eq("owner", owner) \
        .eq("kind", ai_service.CLASSIFY_KIND) \
        .eq("source_hash", src_hash) \
        .order("created_at", desc=True).limit(1).execute()
    if cache.data:
        payload = cache.data[0].get("payload") or {}
        sug = payload.get("suggestion") or ai_service._empty_classify()
        return {"inbox_id": inbox_row.get("id"), "cached": True, **sug}

    suggestion = ai_service.classify_inbox_item(content, categories, owner=owner)

    supabase.table("personal_ai_summaries").insert({
        "owner": owner,
        "kind": ai_service.CLASSIFY_KIND,
        "source_hash": src_hash,
        "payload": {"suggestion": suggestion},
    }).execute()

    return {"inbox_id": inbox_row.get("id"), "cached": False, **suggestion}


@app.post("/api/me/inbox/{inbox_id}/ai-classify")
def ai_classify_inbox(inbox_id: int, user: dict = Depends(get_current_user)):
    """단일 Inbox 항목 분류 — 제목/카테고리/우선순위/태그 제안."""
    _require_ai()
    owner = user["character_name"]
    row = supabase.table("personal_inbox") \
        .select("*").eq("id", inbox_id).eq("owner", owner).execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="Inbox 항목을 찾을 수 없습니다")
    cats_rows = supabase.table("personal_categories") \
        .select("name").eq("owner", owner).execute().data or []
    cat_names = [c["name"] for c in cats_rows if c.get("name")]
    try:
        return _classify_one_inbox(row.data[0], cat_names, owner)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 호출 실패: {str(e)[:200]}")


@app.post("/api/me/inbox/bulk-ai-classify")
def ai_classify_inbox_bulk(user: dict = Depends(get_current_user)):
    """미처리 Inbox 전체를 분류. 캐시된 건 모델 호출 없이 즉시 반환."""
    _require_ai()
    owner = user["character_name"]
    items = supabase.table("personal_inbox") \
        .select("*").eq("owner", owner).eq("processed", False) \
        .order("created_at", desc=True).limit(50).execute().data or []
    if not items:
        return {"results": [], "total": 0}

    cats_rows = supabase.table("personal_categories") \
        .select("name").eq("owner", owner).execute().data or []
    cat_names = [c["name"] for c in cats_rows if c.get("name")]

    results = []
    for it in items:
        try:
            results.append(_classify_one_inbox(it, cat_names, owner))
        except Exception as e:
            results.append({
                "inbox_id": it.get("id"),
                "error": str(e)[:200],
                **ai_service._empty_classify(),
            })
    return {"results": results, "total": len(results)}


@app.post("/api/me/search")
def personal_search(req: PersonalSearchRequest, user: dict = Depends(get_current_user)):
    """자연어로 하루 로그 검색 + 답변 (Claude haiku)."""
    _require_ai()
    owner = user["character_name"]
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="검색어를 입력해주세요")
    if len(query) > 500:
        raise HTTPException(status_code=400, detail="검색어가 너무 깁니다 (500자 이내)")

    days = max(7, min(req.days or 90, 365))
    start = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")

    logs = supabase.table("personal_daily_logs") \
        .select("log_date, content") \
        .eq("owner", owner) \
        .gte("log_date", start) \
        .order("log_date", desc=True) \
        .limit(180).execute().data or []

    try:
        result = ai_service.smart_search(query, logs, owner=owner)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 호출 실패: {str(e)[:200]}")

    return {
        "status": "ok",
        "query": query,
        "days": days,
        "logs_searched": len(logs),
        **result,
    }


# ════════════════════════════════════════════════════════
# Phase 7 — Dashboard briefing + Daily auto-template
# ════════════════════════════════════════════════════════

def _briefing_stats(owner: str) -> tuple[dict, dict]:
    """Compute stats + sample for the dashboard briefing."""
    today = datetime.now().date().strftime("%Y-%m-%d")
    tasks = supabase.table("personal_tasks") \
        .select("*").eq("owner", owner).execute().data or []
    open_tasks = [t for t in tasks if t.get("status") != "done"]

    today_due = [
        t for t in open_tasks
        if t.get("due_date") and t["due_date"] <= today
    ]
    today_due_today_only = [
        t for t in open_tasks if t.get("due_date") == today
    ]

    in_3days = (datetime.now().date() + timedelta(days=3)).strftime("%Y-%m-%d")
    at_risk = [
        t for t in open_tasks
        if t.get("priority") == "high"
        and t.get("due_date")
        and t["due_date"] <= in_3days
    ]

    inbox_unprocessed = supabase.table("personal_inbox") \
        .select("id", count="exact").eq("owner", owner).eq("processed", False).execute()
    inbox_count = inbox_unprocessed.count or 0

    projects_active = supabase.table("personal_projects") \
        .select("id", count="exact").eq("owner", owner).eq("status", "active").execute()
    projects_count = projects_active.count or 0

    last_log = supabase.table("personal_daily_logs") \
        .select("content, log_date").eq("owner", owner) \
        .order("log_date", desc=True).limit(1).execute()
    log_excerpt = ""
    if last_log.data:
        log_excerpt = (last_log.data[0].get("content") or "")[:600]

    stats = {
        "today_due": len(today_due),
        "today_due_only": len(today_due_today_only),
        "inbox_unprocessed": inbox_count,
        "projects_active": projects_count,
        "at_risk": len(at_risk),
    }
    sample = {
        "today_tasks": [t.get("title") for t in today_due][:8],
        "recent_log_excerpt": log_excerpt,
    }
    return stats, sample


def _briefing_signature(stats: dict, sample: dict) -> str:
    """Hash that changes whenever briefing inputs change — for daily caching."""
    today = datetime.now().date().strftime("%Y-%m-%d")
    sig = json.dumps({
        "d": today,
        "s": stats,
        "tt": sample.get("today_tasks", []),
        "le": ai_service.content_hash(sample.get("recent_log_excerpt", "")),
    }, ensure_ascii=False, sort_keys=True)
    return ai_service.content_hash(sig)


@app.post("/api/me/dashboard-briefing")
def dashboard_briefing(force: bool = False, user: dict = Depends(get_current_user)):
    """오늘의 브리핑 — Claude haiku 1~3문장 + 핵심 숫자 카드.

    동일한 입력(같은 날 + 같은 stats/log)은 24h 캐시 재사용.
    AI 비활성 시: text 는 비고 numbers/at_risk 만 반환 (graceful 503-style).
    """
    owner = user["character_name"]
    stats, sample = _briefing_stats(owner)

    base = {
        "today": datetime.now().date().strftime("%Y-%m-%d"),
        "numbers": {
            "today_due": stats["today_due"],
            "inbox_unprocessed": stats["inbox_unprocessed"],
            "projects_active": stats["projects_active"],
            "at_risk": stats["at_risk"],
        },
    }

    if not ai_service.is_enabled():
        return {**base, "text": "", "ai_enabled": False, "cached": False}

    sig = _briefing_signature(stats, sample)

    if not force:
        cache = supabase.table("personal_ai_summaries") \
            .select("payload, created_at") \
            .eq("owner", owner) \
            .eq("kind", ai_service.BRIEFING_KIND) \
            .eq("source_hash", sig) \
            .order("created_at", desc=True).limit(1).execute()
        if cache.data:
            row = cache.data[0]
            payload = row.get("payload") or {}
            return {
                **base,
                "text": payload.get("text") or "",
                "ai_enabled": True,
                "cached": True,
                "generated_at": row.get("created_at"),
            }

    try:
        text = ai_service.dashboard_briefing(stats, sample, owner=owner)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 호출 실패: {str(e)[:200]}")

    supabase.table("personal_ai_summaries").insert({
        "owner": owner,
        "kind": ai_service.BRIEFING_KIND,
        "source_hash": sig,
        "payload": {"text": text, "stats": stats},
    }).execute()

    return {**base, "text": text, "ai_enabled": True, "cached": False}


def _project_retro_signature(project: dict, tasks: list[dict]) -> str:
    """프로젝트 + 작업(계획/실제 일정·상태) 상태의 해시 — 변경 없으면 캐시 재사용."""
    parts = [
        str(project.get("name")), str(project.get("description")),
        str(project.get("status")), str(project.get("start_date")),
        str(project.get("end_date")),
    ]
    for t in sorted(tasks, key=lambda x: x.get("id", 0)):
        parts.append("|".join(str(t.get(k)) for k in (
            "id", "title", "status", "start_date", "due_date",
            "actual_start_date", "actual_end_date",
        )))
    return ai_service.content_hash("\n".join(parts))


@app.post("/api/me/projects/{project_id}/retrospective")
def project_retrospective(project_id: int, force: bool = False,
                          user: dict = Depends(get_current_user)):
    """프로젝트 AI 회고 — 계획 대비 실제 일정·완료율을 보고 회고문 생성.

    같은 상태(작업 일정/상태 변화 없음)는 캐시 재사용. AI 비활성 시 graceful.
    """
    owner = user["character_name"]
    proj = supabase.table("personal_projects") \
        .select("*").eq("id", project_id).eq("owner", owner).execute()
    if not proj.data:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다")
    project = proj.data[0]
    all_tasks = supabase.table("personal_tasks").select("*") \
        .eq("owner", owner).eq("project_id", project_id).execute().data or []
    tasks = [t for t in all_tasks if not t.get("parent_task_id")]

    if not ai_service.is_enabled():
        return {"text": "", "ai_enabled": False, "cached": False}
    if not tasks:
        return {"text": "", "ai_enabled": True, "cached": False, "empty": True}

    sig = _project_retro_signature(project, tasks)
    if not force:
        cache = supabase.table("personal_ai_summaries") \
            .select("payload, created_at") \
            .eq("owner", owner) \
            .eq("kind", ai_service.RETRO_KIND) \
            .eq("source_hash", sig) \
            .order("created_at", desc=True).limit(1).execute()
        if cache.data:
            row = cache.data[0]
            return {
                "text": (row.get("payload") or {}).get("text") or "",
                "ai_enabled": True, "cached": True,
                "generated_at": row.get("created_at"),
            }

    try:
        text = ai_service.project_retrospective(project, tasks, owner=owner)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 호출 실패: {str(e)[:200]}")

    supabase.table("personal_ai_summaries").insert({
        "owner": owner,
        "kind": ai_service.RETRO_KIND,
        "source_hash": sig,
        "payload": {"text": text},
    }).execute()
    return {"text": text, "ai_enabled": True, "cached": False}


@app.get("/api/me/daily-logs/{log_date}/auto-template")
def daily_log_auto_template(log_date: str, user: dict = Depends(get_current_user)):
    """오늘 완료한 task / 오늘 마감 / 진행 중 프로젝트 기반 자동 초안 생성.

    AI 호출 없음 — 결정론적 템플릿. 빈 textarea 채우는 용도.
    """
    owner = user["character_name"]
    tasks = supabase.table("personal_tasks") \
        .select("title, status, due_date, updated_at") \
        .eq("owner", owner).execute().data or []

    completed_today = [
        t.get("title") for t in tasks
        if t.get("status") == "done"
        and (t.get("updated_at") or "").startswith(log_date)
    ]
    today_due = [
        t.get("title") for t in tasks
        if t.get("status") != "done" and t.get("due_date") == log_date
    ]
    projects = supabase.table("personal_projects") \
        .select("name").eq("owner", owner).eq("status", "active") \
        .order("sort_order").limit(8).execute().data or []
    project_names = [p.get("name") for p in projects if p.get("name")]

    template = ai_service.daily_auto_template(
        log_date,
        [t for t in completed_today if t],
        [t for t in today_due if t],
        project_names,
    )
    return {
        "log_date": log_date,
        "template": template,
        "completed_count": len([t for t in completed_today if t]),
        "due_today_count": len([t for t in today_due if t]),
    }


# ════════════════════════════════════════════════════════
# Phase 8 — AI 토큰·비용 사용량 추적 (personal_ai_usage)
# ════════════════════════════════════════════════════════

import ai_pricing  # noqa: E402

_USAGE_CACHE: dict[str, tuple[float, dict]] = {}
_USAGE_CACHE_TTL = 60.0  # seconds


def _last_day_of_month(d) -> int:
    """주어진 날짜가 속한 월의 마지막 일(28~31) 반환."""
    if d.month == 12:
        return 31
    next_first = d.replace(day=1).replace(month=d.month + 1)
    last = next_first - timedelta(days=1)
    return last.day


def _aggregate_rows(rows: list[dict], start_iso: str | None = None) -> dict:
    """rows 중 created_at >= start_iso 인 것만 합산. start_iso=None 이면 전체."""
    calls = 0
    tin = 0
    tout = 0
    cost = 0.0
    for r in rows:
        created = r.get("created_at") or ""
        if start_iso and created < start_iso:
            continue
        calls += 1
        tin += int(r.get("input_tokens") or 0)
        tout += int(r.get("output_tokens") or 0)
        try:
            cost += float(r.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "calls": calls,
        "tokens_in": tin,
        "tokens_out": tout,
        "cost_usd": round(cost, 6),
    }


@app.get("/api/me/ai-usage")
def get_ai_usage(user: dict = Depends(get_current_user)):
    """이번 달 / 이번 주 / 오늘 AI 호출·토큰·비용 통계.

    빈 응답이라도 200 으로 내려 위젯이 항상 떠 있게 한다.
    60초 캐시 (owner 별).
    """
    import time
    owner = user["character_name"]

    # owner 캐시
    cached = _USAGE_CACHE.get(owner)
    now_ts = time.time()
    if cached and (now_ts - cached[0]) < _USAGE_CACHE_TTL:
        return cached[1]

    now = datetime.now()
    today = now.date()
    today_iso = today.strftime("%Y-%m-%d")
    month_start = today.replace(day=1)
    month_start_iso = month_start.strftime("%Y-%m-%dT00:00:00")
    # 이번 주 = 월요일 시작
    week_start = today - timedelta(days=today.weekday())
    week_start_iso = week_start.strftime("%Y-%m-%dT00:00:00")
    today_start_iso = today.strftime("%Y-%m-%dT00:00:00")
    last30_start = today - timedelta(days=29)
    last30_start_iso = last30_start.strftime("%Y-%m-%dT00:00:00")

    # 이번 달 + 지난 30일 둘 다 커버하기 위해 더 이른 시작점 기준으로 한 번에 가져옴
    fetch_from_iso = min(month_start_iso, last30_start_iso)

    # personal_ai_usage 테이블이 아직 마이그레이트 안 됐을 수도 있다.
    # 그래도 위젯이 깨지지 않도록 빈 통계 응답.
    table_missing = False
    try:
        rows = supabase.table("personal_ai_usage") \
            .select("created_at, kind, input_tokens, output_tokens, cost_usd") \
            .eq("owner", owner) \
            .gte("created_at", fetch_from_iso) \
            .order("created_at", desc=True) \
            .limit(5000) \
            .execute().data or []
    except Exception as e:
        msg = str(e).lower()
        if "personal_ai_usage" in msg or "does not exist" in msg or "schema cache" in msg:
            table_missing = True
            rows = []
        else:
            raise

    today_agg = _aggregate_rows(rows, today_start_iso)
    week_agg = _aggregate_rows(rows, week_start_iso)
    month_agg = _aggregate_rows(rows, month_start_iso)

    # 한도 / 진행률
    limit_usd = ai_pricing.monthly_budget_usd()
    pct = 0.0
    if limit_usd > 0:
        pct = round((month_agg["cost_usd"] / limit_usd) * 100.0, 2)
    last_day = _last_day_of_month(today)
    days_until_reset = max(1, last_day - today.day + 1)

    # 종류별 (이번 달 기준)
    by_kind_map: dict[str, dict] = {}
    for r in rows:
        if (r.get("created_at") or "") < month_start_iso:
            continue
        k = r.get("kind") or "other"
        slot = by_kind_map.setdefault(k, {"calls": 0, "cost_usd": 0.0,
                                          "tokens_in": 0, "tokens_out": 0})
        slot["calls"] += 1
        slot["tokens_in"] += int(r.get("input_tokens") or 0)
        slot["tokens_out"] += int(r.get("output_tokens") or 0)
        try:
            slot["cost_usd"] += float(r.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    by_kind = [
        {"kind": k, **v, "cost_usd": round(v["cost_usd"], 6)}
        for k, v in sorted(by_kind_map.items(), key=lambda kv: -kv[1]["cost_usd"])
    ]

    # 지난 30일 일별 (오늘 포함, 데이터 없는 날도 0 으로 채움)
    daily_map: dict[str, float] = {}
    daily_calls: dict[str, int] = {}
    for r in rows:
        created = r.get("created_at") or ""
        if created < last30_start_iso:
            continue
        d = created[:10]
        try:
            daily_map[d] = daily_map.get(d, 0.0) + float(r.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
        daily_calls[d] = daily_calls.get(d, 0) + 1
    daily_last_30 = []
    for i in range(30):
        d = (last30_start + timedelta(days=i)).strftime("%Y-%m-%d")
        daily_last_30.append({
            "date": d,
            "cost_usd": round(daily_map.get(d, 0.0), 6),
            "calls": daily_calls.get(d, 0),
        })

    payload = {
        "today": {**today_agg, "date": today_iso},
        "this_week": {**week_agg, "start": week_start.strftime("%Y-%m-%d")},
        "this_month": {
            **month_agg,
            "start": month_start.strftime("%Y-%m-%d"),
            "limit_usd": round(limit_usd, 2),
            "pct": pct,
            "days_until_reset": days_until_reset,
        },
        "by_kind": by_kind,
        "daily_last_30": daily_last_30,
        "model": ai_service.CLAUDE_MODEL,
        "price": {
            "input_per_m_usd": ai_pricing.PRICE_INPUT_PER_M_USD,
            "output_per_m_usd": ai_pricing.PRICE_OUTPUT_PER_M_USD,
        },
        "ai_enabled": ai_service.is_enabled(),
        "table_missing": table_missing,
    }

    _USAGE_CACHE[owner] = (now_ts, payload)
    return payload

# ── 개인 업무 디지스트 (테스트용 수동 트리거) ──────────────
@app.post("/api/me/digest/send-test")
def send_digest_test(user: dict = Depends(get_current_user)):
    from email_digest import send_digest
    try:
        result = send_digest(user["character_name"])
        return {"ok": True, "id": result.get("id")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/me/digest/preview")
def preview_digest(user: dict = Depends(get_current_user)):
    """디지스트 HTML 미리보기 (브라우저에서 GET 가능)."""
    from email_digest import build_digest
    from fastapi.responses import HTMLResponse
    digest = build_digest(user["character_name"])
    return HTMLResponse(content=digest["html"])


# ── 콘텐츠 일정 API ───────────────────────────────────────────
# 설계: guild-app-54/docs/일정_백엔드화_설계.md
# 게임 시각은 전부 KST. Railway는 UTC라 반드시 KST로 계산/저장.
# KST / 일정 빌드 로직(_expand_rule, build_schedule)은 schedule_logic.py 로 분리 — 일정 푸시와 공용.


def _norm_kst(iso: str) -> str:
    """ISO 문자열을 tz-aware로 정규화. 오프셋 없으면 KST로 간주(운영진 wall-clock 입력 방어)."""
    try:
        dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"잘못된 날짜 형식: {iso}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.isoformat()


@app.get("/api/schedule")
def get_schedule(weeks: int = 6):
    """앱 홈/일정 화면용. 상시(규칙 펼침) + 시즌(occurrences) 병합해 회차 리스트 반환(camelCase).
    비로그인 허용. id: 시즌=int, 상시=합성문자열(content@start).
    실제 계산은 schedule_logic.build_schedule (일정 푸시 스케줄러와 공용)."""
    return build_schedule(weeks)


# ── 운영진: 시즌 회차 관리 ────────────────────────────────────

class OccurrenceIn(BaseModel):
    content_id: str = Field(alias="contentId")
    round_label: Optional[str] = Field(default=None, alias="roundLabel")
    start_at: str = Field(alias="startAt")
    end_at: str = Field(alias="endAt")
    model_config = {"populate_by_name": True}


class OccurrencePatch(BaseModel):
    round_label: Optional[str] = Field(default=None, alias="roundLabel")
    start_at: Optional[str] = Field(default=None, alias="startAt")
    end_at: Optional[str] = Field(default=None, alias="endAt")
    model_config = {"populate_by_name": True}


@app.get("/api/admin/contents")
def admin_list_contents(admin: dict = Depends(require_admin)):
    """운영진 입력 화면용 콘텐츠 메타 목록(전체). snake_case 그대로."""
    return (supabase.table("contents").select("*").order("sort_order").execute().data) or []


@app.post("/api/admin/occurrences")
def admin_create_occurrences(items: list[OccurrenceIn], admin: dict = Depends(require_admin)):
    """시즌 회차 등록. 한 시즌(여러 회차)을 배열로 한 번에. 상시(always) 콘텐츠엔 등록 불가."""
    if not items:
        raise HTTPException(status_code=400, detail="등록할 회차가 없습니다")
    contents = {c["id"]: c for c in
                (supabase.table("contents").select("id,type").execute().data or [])}
    rows = []
    for it in items:
        c = contents.get(it.content_id)
        if not c:
            raise HTTPException(status_code=400, detail=f"없는 콘텐츠: {it.content_id}")
        if c["type"] != "season":
            raise HTTPException(status_code=400, detail=f"상시 콘텐츠({it.content_id})는 회차 등록 대상이 아닙니다")
        start, end = _norm_kst(it.start_at), _norm_kst(it.end_at)
        if start >= end:
            raise HTTPException(status_code=400, detail="시작 시각이 종료 시각보다 빠를 수 없습니다")
        rows.append({
            "content_id": it.content_id, "round_label": it.round_label,
            "start_at": start, "end_at": end,
        })
    result = supabase.table("occurrences").insert(rows).execute()
    return {"status": "ok", "inserted": len(result.data or []), "rows": result.data or []}


@app.patch("/api/admin/occurrences/{occ_id}")
def admin_update_occurrence(occ_id: int, req: OccurrencePatch, admin: dict = Depends(require_admin)):
    """시즌 회차 수정(부분)."""
    patch = {}
    if req.round_label is not None:
        patch["round_label"] = req.round_label
    if req.start_at is not None:
        patch["start_at"] = _norm_kst(req.start_at)
    if req.end_at is not None:
        patch["end_at"] = _norm_kst(req.end_at)
    if not patch:
        raise HTTPException(status_code=400, detail="수정할 내용이 없습니다")
    if "start_at" in patch and "end_at" in patch and patch["start_at"] >= patch["end_at"]:
        raise HTTPException(status_code=400, detail="시작 시각이 종료 시각보다 빠를 수 없습니다")
    result = supabase.table("occurrences").update(patch).eq("id", occ_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="해당 회차를 찾을 수 없습니다")
    return result.data[0]


@app.get("/api/admin/occurrences")
def admin_list_occurrences(content_id: Optional[str] = None, admin: dict = Depends(require_admin)):
    """운영진 일정 관리 화면용 — 등록된 시즌 회차 전체(과거 포함, 최신순). content_id로 필터 가능.
    /api/schedule(window 한정)과 달리 과거 회차도 보여 삭제 가능하게 함."""
    q = supabase.table("occurrences").select("*").order("start_at", desc=True)
    if content_id:
        q = q.eq("content_id", content_id)
    rows = q.execute().data or []
    return [{
        "id": r["id"], "contentId": r["content_id"], "roundLabel": r.get("round_label"),
        "startAt": r["start_at"], "endAt": r["end_at"],
    } for r in rows]


@app.delete("/api/admin/occurrences/{occ_id}")
def admin_delete_occurrence(occ_id: int, admin: dict = Depends(require_admin)):
    """시즌 회차 삭제."""
    supabase.table("occurrences").delete().eq("id", occ_id).execute()
    return {"status": "ok"}


# ── 푸시 토큰 등록/해제 ────────────────────────────────────────
# 앱이 로그인 직후 토큰 등록, 로그아웃/거부 시 해제. 일정 푸시는 push_send.py 스케줄러가 발송.

class PushRegisterBody(BaseModel):
    token: str
    platform: Optional[str] = None


class PushUnregisterBody(BaseModel):
    token: str


@app.post("/api/push/register")
def push_register(body: PushRegisterBody, user: dict = Depends(get_current_user)):
    """Expo push token 저장(토큰 UNIQUE upsert → 같은 토큰이면 캐릭터명/플랫폼만 갱신)."""
    supabase.table("push_tokens").upsert({
        "character_name": user["character_name"],
        "token": body.token,
        "platform": body.platform,
    }, on_conflict="token").execute()
    return {"status": "ok"}


@app.post("/api/push/unregister")
def push_unregister(body: PushUnregisterBody, user: dict = Depends(get_current_user)):
    """로그아웃/알림 끄기 시 해당 토큰 삭제."""
    supabase.table("push_tokens").delete().eq("token", body.token).execute()
    return {"status": "ok"}


# ── 길드 가입 문의 (비로그인 제출 → 운영진 검토 + 푸시) ──────────────────
# 외부인이 앱 로그인 화면에서 "길드 가입 문의"로 남긴 글을 저장하고,
# 운영진(admin/superadmin)의 푸시 토큰에만 알림을 보낸다. 실제 합류는
# 기존 흐름(members/temp 등록 → 회원가입 → 승인)으로 진행하고, 여기선
# 문의 인박스 + 수락/거절 상태만 관리한다.

class JoinInquiryIn(BaseModel):
    character_name: str = Field(alias="characterName")
    power_text: Optional[str] = Field(default=None, alias="powerText")
    contact: Optional[str] = None
    message: Optional[str] = None
    model_config = {"populate_by_name": True}


class JoinInquiryStatus(BaseModel):
    status: str  # pending | accepted | rejected


def _notify_admins_join_inquiry(name: str):
    """가입 문의 접수 시 운영진 토큰으로 푸시(실패해도 접수는 성공시킨다)."""
    try:
        admins = (supabase.table("users").select("character_name")
                  .in_("role", ["admin", "superadmin"]).execute().data) or []
        admin_names = [a["character_name"] for a in admins]
        if not admin_names:
            return
        rows = (supabase.table("push_tokens").select("token")
                .in_("character_name", admin_names).execute().data) or []
        tokens = [r["token"] for r in rows]
        if tokens:
            _send(tokens, "📨 새 길드 가입 문의",
                  f"{name}님이 가입 문의를 남겼어요. 확인해보세요!",
                  {"route": "/admin/join-inquiries"})
    except Exception as e:
        print(f"[가입문의] 운영진 푸시 실패: {e}")


@app.post("/api/join-inquiries")
def create_join_inquiry(req: JoinInquiryIn):
    """길드 가입 문의 접수 (비로그인). 저장 후 운영진에 푸시."""
    name = (req.character_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="캐릭터명(닉네임)을 입력해주세요")
    if len(name) > 50:
        raise HTTPException(status_code=400, detail="캐릭터명이 너무 깁니다")
    # 같은 캐릭터명으로 미처리(pending) 문의가 이미 있으면 중복 접수 방지
    dup = (supabase.table("join_inquiries").select("id")
           .eq("character_name", name).eq("status", "pending").limit(1).execute().data)
    if dup:
        raise HTTPException(status_code=409, detail="이미 접수된 문의가 있어요. 운영진 확인을 기다려주세요")
    supabase.table("join_inquiries").insert({
        "character_name": name,
        "power_text": ((req.power_text or "").strip() or None),
        "contact": ((req.contact or "").strip() or None),
        "message": ((req.message or "").strip() or None),
        "status": "pending",
    }).execute()
    _notify_admins_join_inquiry(name)
    return {"status": "ok", "message": "가입 문의가 접수됐어요. 운영진이 확인 후 연락드릴게요!"}


@app.get("/api/admin/join-inquiries")
def admin_list_join_inquiries(status: Optional[str] = None, admin: dict = Depends(require_admin)):
    """가입 문의 목록(운영진). 기본 최신순 전체, status로 필터."""
    q = supabase.table("join_inquiries").select("*").order("created_at", desc=True)
    if status:
        q = q.eq("status", status)
    rows = q.execute().data or []
    return [{
        "id": r["id"], "characterName": r["character_name"],
        "powerText": r.get("power_text"), "contact": r.get("contact"),
        "message": r.get("message"), "status": r["status"],
        "createdAt": r["created_at"],
    } for r in rows]


@app.patch("/api/admin/join-inquiries/{inq_id}")
def admin_update_join_inquiry(inq_id: int, req: JoinInquiryStatus,
                              admin: dict = Depends(require_admin)):
    """문의 상태 변경(수락/거절/대기로 되돌리기)."""
    if req.status not in ("pending", "accepted", "rejected"):
        raise HTTPException(status_code=400, detail="잘못된 상태입니다")
    result = (supabase.table("join_inquiries").update({"status": req.status})
              .eq("id", inq_id).execute())
    if not result.data:
        raise HTTPException(status_code=404, detail="해당 문의를 찾을 수 없습니다")
    return result.data[0]
