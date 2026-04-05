from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
            "hasSnapshot": snap is not None,    # 스냅샷 존재 여부
            "isMaster": cur.get("is_master", False),
            "detailUrl": cur.get("detail_url"),
        })

    # 성장량 기준 정렬 (null은 뒤로)
    result.sort(key=lambda x: x.get("monthlyDiff") or -999999999, reverse=True)
    return result


@app.get("/api/notices")
def get_notices():
    result = supabase.table("notices")\
        .select("*")\
        .order("created_at", desc=True)\
        .execute()
    return result.data


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