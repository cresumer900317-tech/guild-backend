from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import bcrypt
from database import supabase
from scheduler import start_scheduler
from datetime import datetime


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
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "ok", "message": "친구패밀리 백엔드 작동 중!"}


@app.get("/api/ranking")
def get_ranking():
    result = supabase.table("members")\
        .select("*")\
        .order("server_rank")\
        .execute()
    return to_camel(result.data)


@app.get("/api/members")
def get_members():
    result = supabase.table("members")\
        .select("*")\
        .execute()
    return to_camel(result.data)


@app.get("/api/weekly")
def get_weekly():
    result = supabase.table("members")\
        .select("*")\
        .order("weekly_diff", desc=True)\
        .execute()
    return to_camel(result.data)


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
    current_result = supabase.table("members").select("*").execute()
    current_members = {m["name"]: m for m in current_result.data}

    # 이번 달 월초 스냅샷
    snapshot_result = supabase.table("monthly_snapshots")\
        .select("*")\
        .eq("snapshot_month", snapshot_month)\
        .execute()
    snapshot_map = {s["name"]: s for s in snapshot_result.data}

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
            "monthlyDiff": monthly_diff,        # 월간 성장량 (숫자, snap 없으면 null)
            "growthRate": growth_rate,          # 월간 성장률 (%, snap 없으면 null)
            "snapshotMonth": snapshot_month,    # "2025-04"
            "monthlyServerDiff": monthly_server_diff,  # 월간 서버 순위 변동
            "hasSnapshot": snap is not None,    # 스냅샷 존재 여부
            "isMaster": cur.get("is_master", False),
            "detailUrl": cur.get("detail_url"),
        })

    # 성장량 기준 정렬 (null은 뒤로)
    result.sort(key=lambda x: x.get("monthlyDiff") or -999999999, reverse=True)
    return result


@app.get("/api/home-summary")
def get_home_summary():
    result = supabase.table("members").select("*").execute()
    members = result.data
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
    friends_result = supabase.table("members")        .select("*")        .eq("guild", "친구들")        .execute()
    friends_members = sorted(
        friends_result.data or [],
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
def upsert_contribution(payload: dict):
    """공헌도 입력/수정 (upsert)"""
    month = payload.get("month")
    guild_name = payload.get("guild_name")
    member_name = payload.get("member_name")
    contribution = int(payload.get("contribution", 0))

    if not all([month, guild_name, member_name]):
        raise HTTPException(status_code=400, detail="month, guild_name, member_name 필수")

    supabase.table("guild_contributions").upsert({
        "month": month,
        "guild_name": guild_name,
        "member_name": member_name,
        "contribution": contribution,
    }, on_conflict="month,guild_name,member_name").execute()

    return {"status": "ok", "message": f"{member_name} 공헌도 저장 완료"}


@app.delete("/api/contributions")
def delete_contribution(month: str, guild_name: str, member_name: str):
    """공헌도 삭제"""
    supabase.table("guild_contributions")        .delete()        .eq("month", month)        .eq("guild_name", guild_name)        .eq("member_name", member_name)        .execute()
    return {"status": "ok"}


# ── 회원 API ──────────────────────────────────────────────────

@app.post("/api/auth/register")
def register(payload: dict):
    """회원가입"""
    character_name = (payload.get("character_name") or "").strip()
    password = (payload.get("password") or "").strip()

    if not character_name or not password:
        raise HTTPException(status_code=400, detail="캐릭터명과 비밀번호를 입력해주세요")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다")

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

    supabase.table("users").insert({
        "character_name": character_name,
        "password_hash": pw_hash,
        "guild": member.get("guild", ""),
        "status": "pending",
        "role": "member",
    }).execute()

    return {"status": "ok", "message": "가입 신청이 완료됐습니다. 운영진 승인 후 이용 가능합니다"}


@app.post("/api/auth/login")
def login(payload: dict):
    """로그인"""
    character_name = (payload.get("character_name") or "").strip()
    password = (payload.get("password") or "").strip()

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

    return {
        "status": "ok",
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
def approve_user(payload: dict):
    """유저 승인 (어드민용)"""
    character_name = payload.get("character_name")
    guild = payload.get("guild")
    if not character_name:
        raise HTTPException(status_code=400, detail="character_name 필수")

    supabase.table("users").update({
        "status": "active",
        "guild": guild,
        "approved_at": datetime.now().isoformat(),
    }).eq("character_name", character_name).execute()

    return {"status": "ok", "message": f"{character_name} 승인 완료"}


@app.post("/api/auth/deactivate")
def deactivate_user(payload: dict):
    """유저 비활성화 (길드 탈퇴 등)"""
    character_name = payload.get("character_name")
    if not character_name:
        raise HTTPException(status_code=400, detail="character_name 필수")

    supabase.table("users").update({
        "status": "inactive",
    }).eq("character_name", character_name).execute()

    return {"status": "ok", "message": f"{character_name} 비활성화 완료"}


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
def create_notice(payload: dict):
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    category = payload.get("category", "공지")
    author = payload.get("author", "운영진")
    author_guild = payload.get("author_guild", "")
    is_pinned = payload.get("is_pinned", False)
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 입력해주세요")
    result = supabase.table("notices").insert({
        "title": title, "content": content, "category": category,
        "author": author, "author_guild": author_guild, "is_pinned": is_pinned,
    }).execute()
    return result.data[0] if result.data else {}

@app.delete("/api/notices/{notice_id}")
def delete_notice(notice_id: int):
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
def create_tip(payload: dict):
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    category = payload.get("category", "일반")
    author = payload.get("author", "")
    author_guild = payload.get("author_guild", "")
    if not title or not content:
        raise HTTPException(status_code=400, detail="제목과 내용을 입력해주세요")
    if not author:
        raise HTTPException(status_code=400, detail="로그인이 필요합니다")
    result = supabase.table("tips").insert({
        "title": title, "content": content, "category": category,
        "author": author, "author_guild": author_guild, "likes": 0,
    }).execute()
    return result.data[0] if result.data else {}

@app.post("/api/tips/{tip_id}/like")
def like_tip(tip_id: int):
    result = supabase.table("tips").select("likes").eq("id", tip_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 게시글")
    current = result.data[0]["likes"] or 0
    supabase.table("tips").update({"likes": current + 1}).eq("id", tip_id).execute()
    return {"likes": current + 1}

@app.delete("/api/tips/{tip_id}")
def delete_tip(tip_id: int):
    supabase.table("tips").delete().eq("id", tip_id).execute()
    return {"status": "ok"}


@app.post("/api/auth/role")
def change_role(payload: dict):
    """role 변경 (superadmin 전용)"""
    character_name = payload.get("character_name")
    new_role = payload.get("role")
    if not character_name or new_role not in ["member", "admin", "superadmin"]:
        raise HTTPException(status_code=400, detail="잘못된 요청")
    supabase.table("users").update({"role": new_role})        .eq("character_name", character_name).execute()
    return {"status": "ok", "message": f"{character_name} → {new_role}"}


# ── 방문자 API ──────────────────────────────────────────────

@app.post("/api/visitors/ping")
def visitor_ping(payload: dict):
    """방문자 핑 (페이지 로드시 호출)"""
    session_id = payload.get("session_id", "")
    character_name = payload.get("character_name", "guest")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 필수")

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # visitors 테이블 upsert (session_id 기준)
    existing = supabase.table("visitors")        .select("id, created_at")        .eq("session_id", session_id)        .execute()

    is_new = not existing.data

    supabase.table("visitors").upsert({
        "session_id": session_id,
        "character_name": character_name,
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