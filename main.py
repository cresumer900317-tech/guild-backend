from contextlib import asynccontextmanager
from typing import Optional, Literal
import os
import jwt
from pydantic import BaseModel, field_validator
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import bcrypt
from database import supabase
from scheduler import start_scheduler
from datetime import datetime, timedelta

JWT_SECRET = os.environ.get("JWT_SECRET", "changeme-dev-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24


def create_access_token(character_name: str, role: str) -> str:
    payload = {
        "sub": character_name,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(authorization: str = Header(None)) -> dict:
    """Authorization: Bearer <token> 헤더에서 유저 정보 추출"""
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


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """어드민 권한 체크"""
    if current_user["role"] not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
    return current_user


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

class ContributionUpsert(BaseModel):
    month: str
    guild_name: str
    member_name: str
    contribution: int = 0

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
    allow_origin_regex=r".*",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "ok", "message": "친구패밀리 백엔드 작동 중!", "version": "2026-04-15-v3"}


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
def update_pop_rank():
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
    result = supabase.table("members")\
        .select("*")\
        .order("weekly_diff", desc=True)\
        .execute()
    return to_camel(result.data)


@app.post("/api/snapshot-pop-backfill")
def snapshot_pop_backfill():
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
    now = datetime.now()
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
    now = datetime.now()
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

    return {
        "guild_name": "친구패밀리",
        "guild_count": len(guilds),
        "member_count": len(members),
        "avg_power": avg_power,
        "avg_server_rank": avg_rank,
        "top_monthly_growth": top_growth,
    }


@app.post("/api/crawl")
def manual_crawl():
    from scheduler import run_crawl
    run_crawl()
    return {"status": "ok", "message": "크롤링 완료"}


@app.post("/api/snapshot")
def manual_snapshot():
    """수동으로 이번 달 스냅샷 저장 (테스트용)"""
    from scheduler import run_crawl_and_snapshot
    run_crawl_and_snapshot()
    return {"status": "ok", "message": "월간 스냅샷 저장 완료"}


@app.get("/api/rivals")
def get_rivals():
    """
    경쟁 길드 + 친구들(메인 길드) 비교 데이터
    멤버 리스트 포함
    """
    now = datetime.now()

    # ── 친구들 멤버 (members 테이블에서 친구들만) ──
    friends_data = fetch_members_raw(filters="&guild=eq.친구들")
    friends_members = sorted(
        friends_data or [],
        key=lambda m: m.get("power") or 0,
        reverse=True
    )

    friends_total = sum(m.get("power") or 0 for m in friends_members)
    friends_count = len(friends_members)
    friends_avg_level = round(
        sum(m.get("level") or 0 for m in friends_members) / friends_count, 1
    ) if friends_count else 0
    friends_top1 = friends_members[0] if friends_members else {}

    # 월간 성장량 계산
    snapshot_month = now.strftime("%Y-%m")
    snap_result = supabase.table("monthly_snapshots")        .select("name,power")        .eq("snapshot_month", snapshot_month)        .execute()
    snap_map = {s["name"]: s["power"] or 0 for s in (snap_result.data or [])}
    friends_monthly_growth = sum(
        (m.get("power") or 0) - snap_map.get(m["name"], m.get("power") or 0)
        for m in friends_members
        if m["name"] in snap_map
    )

    # 친구들 공헌도 합산
    contrib_month = now.strftime("%Y-%m")
    contrib_result = supabase.table("guild_contributions")        .select("contribution")        .eq("month", contrib_month)        .eq("guild_name", "친구들")        .execute()
    friends_contribution = sum(r.get("contribution", 0) for r in (contrib_result.data or []))

    # 친구들 인기도 합산
    friends_popularity = sum(m.get("popularity") or 0 for m in friends_members)

    # 친구들 성장률
    friends_growth_rate = None
    if friends_monthly_growth and friends_total:
        base = friends_total - friends_monthly_growth
        if base > 0:
            friends_growth_rate = round(friends_monthly_growth / base * 100, 2)

    friends_guild = {
        "guild_name": "친구들",
        "captured_at": now.isoformat(),
        "total_power": friends_total,
        "member_count": friends_count,
        "avg_level": friends_avg_level,
        "monthly_growth": friends_monthly_growth,
        "growth_rate": friends_growth_rate,
        "total_popularity": friends_popularity,
        "total_contribution": friends_contribution,
        "top1_name": friends_top1.get("name", ""),
        "top1_power": friends_top1.get("power", 0),
        "top1_job": friends_top1.get("job", ""),
        "members": [
            {
                "name": m.get("name"),
                "job": m.get("job"),
                "level": m.get("level"),
                "power": m.get("power"),
                "power_text": m.get("power_text"),
                "guild_rank": m.get("guild_rank"),
                "detail_url": m.get("detail_url"),
            }
            for m in friends_members
        ],
    }

    # ── 경쟁 길드 (rival_guilds + rival_members + rival_snapshots) ──
    rival_names = ["싸이월드", "리안"]
    rivals = []
    snap_month = now.strftime("%Y-%m")

    for name in rival_names:
        summary_result = supabase.table("rival_guilds")            .select("*")            .eq("guild_name", name)            .order("captured_at", desc=True)            .limit(1)            .execute()
        if not summary_result.data:
            continue
        summary = summary_result.data[0]

        members_result = supabase.table("rival_members")            .select("*")            .eq("guild_name", name)            .order("guild_rank")            .execute()
        rival_members = members_result.data or []

        avg_level = round(
            sum(m.get("level") or 0 for m in rival_members) / len(rival_members), 1
        ) if rival_members else 0

        total_popularity = sum(m.get("popularity") or 0 for m in rival_members)

        # 월간 성장량 계산 (스냅샷과 현재 비교)
        monthly_growth = None
        growth_rate = None
        snap_result = supabase.table("rival_snapshots")            .select("total_power")            .eq("snapshot_month", snap_month)            .eq("guild_name", name)            .limit(1)            .execute()
        if snap_result.data:
            snap_power = snap_result.data[0]["total_power"] or 0
            current_power = summary.get("total_power") or 0
            if snap_power > 0 and current_power > 0:
                monthly_growth = current_power - snap_power
                growth_rate = round(monthly_growth / snap_power * 100, 2)

        rivals.append({
            **summary,
            "avg_level": avg_level,
            "monthly_growth": monthly_growth,
            "growth_rate": growth_rate,
            "total_popularity": total_popularity,
            "total_contribution": None,
            "members": [
                {
                    "name": m.get("name"),
                    "job": m.get("job"),
                    "level": m.get("level"),
                    "power": m.get("power"),
                    "power_text": m.get("power_text"),
                    "guild_rank": m.get("guild_rank"),
                    "detail_url": None,
                }
                for m in rival_members
            ],
        })

    all_guilds = [friends_guild] + rivals
    all_guilds.sort(key=lambda x: x.get("total_power") or 0, reverse=True)
    return all_guilds


@app.post("/api/rivals/crawl")
def manual_rival_crawl():
    """경쟁 길드 수동 크롤링 (테스트용)"""
    from scheduler import run_rival_crawl
    run_rival_crawl()
    return {"status": "ok", "message": "경쟁 길드 크롤링 완료"}


@app.post("/api/rivals/snapshot")
def manual_rival_snapshot():
    """경쟁 길드 수동 스냅샷 저장 (테스트용)"""
    from scheduler import save_rival_snapshot
    save_rival_snapshot()
    return {"status": "ok", "message": "경쟁 길드 스냅샷 저장 완료"}


# ── 공헌도 API ──────────────────────────────────────────────

@app.get("/api/contributions")
def get_contributions(month: str = None):
    """월별 길드 공헌도 조회"""
    if not month:
        from datetime import datetime
        month = datetime.now().strftime("%Y-%m")
    result = supabase.table("guild_contributions")        .select("*")        .eq("month", month)        .order("contribution", desc=True)        .execute()
    rows = result.data or []

    # 길드별 합산
    from collections import defaultdict
    guild_totals = defaultdict(int)
    guild_members = defaultdict(list)
    for row in rows:
        guild_totals[row["guild_name"]] += row["contribution"]
        guild_members[row["guild_name"]].append({
            "name": row["member_name"],
            "contribution": row["contribution"],
        })

    return {
        "month": month,
        "guilds": [
            {
                "guild_name": g,
                "total": guild_totals[g],
                "members": guild_members[g],
            }
            for g in guild_totals
        ],
        "rows": rows,
    }


@app.post("/api/contributions")
def upsert_contribution(req: ContributionUpsert):
    """공헌도 입력/수정 (upsert)"""
    supabase.table("guild_contributions").upsert({
        "month": req.month,
        "guild_name": req.guild_name,
        "member_name": req.member_name,
        "contribution": req.contribution,
    }, on_conflict="month,guild_name,member_name").execute()

    return {"status": "ok", "message": f"{req.member_name} 공헌도 저장 완료"}


@app.delete("/api/contributions")
def delete_contribution(month: str, guild_name: str, member_name: str):
    """공헌도 삭제"""
    supabase.table("guild_contributions")        .delete()        .eq("month", month)        .eq("guild_name", guild_name)        .eq("member_name", member_name)        .execute()
    return {"status": "ok"}


# ── 회원 API ──────────────────────────────────────────────────

@app.post("/api/auth/register")
def register(req: AuthRequest):
    """회원가입"""
    character_name = req.character_name
    password = req.password

    if not character_name or not password:
        raise HTTPException(status_code=400, detail="캐릭터명과 비밀번호를 입력해주세요")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다")
    if not req.email or not req.email.strip():
        raise HTTPException(status_code=400, detail="이메일을 입력해주세요")
    if not req.birthdate or not req.birthdate.strip():
        raise HTTPException(status_code=400, detail="생년월일을 입력해주세요")

    # 캐릭터명이 실제 길드원인지 확인
    member_result = supabase.table("members")        .select("name, guild")        .eq("name", character_name)        .execute()
    if not member_result.data:
        raise HTTPException(status_code=404, detail="등록된 길드원이 아닙니다. 캐릭터명을 확인해주세요")

    member = member_result.data[0]

    # 이미 가입된 계정인지 확인
    existing = supabase.table("users")        .select("id, status")        .eq("character_name", character_name)        .execute()
    if existing.data:
        status = existing.data[0]["status"]
        if status == "pending":
            raise HTTPException(status_code=409, detail="이미 가입 신청이 접수됐습니다. 운영진 승인을 기다려주세요")
        elif status == "active":
            raise HTTPException(status_code=409, detail="이미 가입된 계정입니다")
        elif status == "inactive":
            raise HTTPException(status_code=403, detail="비활성화된 계정입니다. 운영진에게 문의해주세요")

    # 비밀번호 해시
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    user_data = {
        "character_name": character_name,
        "password_hash": pw_hash,
        "guild": member.get("guild", ""),
        "status": "pending",
        "role": "member",
    }
    if req.email:
        user_data["email"] = req.email.strip()
    if req.birthdate:
        user_data["birthdate"] = req.birthdate.strip()

    supabase.table("users").insert(user_data).execute()

    return {"status": "ok", "message": "가입 신청이 완료됐습니다. 운영진 승인 후 이용 가능합니다"}


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
def get_users(status: str = None):
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


@app.post("/api/auth/init-superadmin")
def init_superadmin():
    """슈퍼어드민 비밀번호 초기화 (일회용)"""
    user = supabase.table("users").select("password_hash").eq("character_name", "친구닷").execute()
    if not user.data:
        raise HTTPException(status_code=404, detail="친구닷 계정을 찾을 수 없습니다")
    # 이미 올바른 비밀번호가 설정되어 있으면 스킵
    existing = user.data[0].get("password_hash", "")
    if existing and bcrypt.checkpw(b"wedding260606", existing.encode()):
        return {"status": "ok", "message": "이미 설정되어 있습니다"}
    pw_hash = bcrypt.hashpw(b"wedding260606", bcrypt.gensalt()).decode()
    supabase.table("users").update({"password_hash": pw_hash}).eq("character_name", "친구닷").execute()
    return {"status": "ok", "message": "친구닷 비밀번호 초기화 완료"}

@app.delete("/api/auth/users/{character_name}")
def delete_user(character_name: str):
    """유저 삭제"""
    supabase.table("users").delete().eq("character_name", character_name).execute()
    return {"status": "ok", "message": f"{character_name} 삭제 완료"}


# ── 공지사항 API ──────────────────────────────────────────────

@app.get("/api/notices")
def get_notices():
    result = supabase.table("notices")        .select("*")        .order("is_pinned", desc=True)        .order("created_at", desc=True)        .execute()
    return result.data or []

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
    return result.data[0] if result.data else {}

@app.delete("/api/notices/{notice_id}")
def delete_notice(notice_id: int, admin: dict = Depends(require_admin)):
    supabase.table("notices").delete().eq("id", notice_id).execute()
    return {"status": "ok"}


# ── 꿀팁 API ──────────────────────────────────────────────────

@app.get("/api/tips")
def get_tips(category: str = None):
    query = supabase.table("tips").select("*").order("created_at", desc=True)
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return result.data or []

@app.post("/api/tips")
def create_tip(req: TipCreate):
    title = req.title.strip()
    content = req.content.strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 입력해주세요")
    if not req.author:
        raise HTTPException(status_code=400, detail="로그인이 필요합니다")
    result = supabase.table("tips").insert({
        "title": title, "content": content, "category": req.category,
        "author": req.author, "author_guild": req.author_guild, "likes": 0, "views": 0,
    }).execute()
    return result.data[0] if result.data else {}

@app.get("/api/tips/{tip_id}")
def get_tip(tip_id: int):
    result = supabase.table("tips").select("*").eq("id", tip_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    return result.data[0]

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
def like_tip(tip_id: int):
    result = supabase.table("tips").select("likes").eq("id", tip_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    current = result.data[0]["likes"] or 0
    supabase.table("tips").update({"likes": current + 1}).eq("id", tip_id).execute()
    return {"likes": current + 1}

@app.post("/api/tips/{tip_id}/view")
def view_tip(tip_id: int):
    result = supabase.table("tips").select("views").eq("id", tip_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    current = result.data[0].get("views") or 0
    supabase.table("tips").update({"views": current + 1}).eq("id", tip_id).execute()
    return {"views": current + 1}

@app.delete("/api/tips/{tip_id}")
def delete_tip(tip_id: int):
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
def get_free_posts():
    result = supabase.table("free_posts").select("*").order("created_at", desc=True).execute()
    return result.data or []

@app.get("/api/free/{post_id}")
def get_free_post(post_id: int):
    result = supabase.table("free_posts").select("*").eq("id", post_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    return result.data[0]

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
def create_free_post(payload: dict):
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    author = payload.get("author", "")
    author_guild = payload.get("author_guild", "")
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 입력해주세요")
    if not author:
        raise HTTPException(status_code=400, detail="로그인이 필요합니다")
    result = supabase.table("free_posts").insert({
        "title": title, "content": content,
        "author": author, "author_guild": author_guild,
        "likes": 0, "views": 0,
    }).execute()
    return result.data[0] if result.data else {}

@app.post("/api/free/{post_id}/like")
def like_free_post(post_id: int):
    result = supabase.table("free_posts").select("likes").eq("id", post_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    current = result.data[0]["likes"] or 0
    supabase.table("free_posts").update({"likes": current + 1}).eq("id", post_id).execute()
    return {"likes": current + 1}

@app.delete("/api/free/{post_id}")
def delete_free_post(post_id: int):
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
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

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
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

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

    return {
        "today": today_count,
        "total": total_count,
        "online": online_count,
        "online_list": [
            {"name": r["character_name"]} for r in online_list
        ],
    }


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
    notes: Optional[str] = ""
    status: Optional[str] = "todo"
    priority: Optional[str] = "medium"
    due_date: Optional[str] = None  # YYYY-MM-DD
    tags: Optional[list[str]] = None
    sort_order: Optional[int] = 0


class PersonalTaskUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    project_id: Optional[int] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None
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
        "notes": req.notes or "",
        "status": status,
        "priority": priority,
        "due_date": req.due_date or None,
        "tags": req.tags or [],
        "sort_order": req.sort_order or 0,
    }
    if status == "done":
        row["completed_at"] = datetime.now().isoformat()
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
        if req.status == "done" and old["status"] != "done":
            updates["completed_at"] = datetime.now().isoformat()
        elif req.status != "done":
            updates["completed_at"] = None
    if req.priority is not None:
        if req.priority not in ALLOWED_TASK_PRIORITY:
            raise HTTPException(status_code=400, detail="잘못된 우선순위입니다")
        updates["priority"] = req.priority
    if req.due_date is not None:
        updates["due_date"] = req.due_date or None
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
        extract = ai_service.extract_from_daily_log(content, log_date)
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
    title = (item.get("title") or "").strip()
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
        result = ai_service.smart_search(query, logs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 호출 실패: {str(e)[:200]}")

    return {
        "status": "ok",
        "query": query,
        "days": days,
        "logs_searched": len(logs),
        **result,
    }