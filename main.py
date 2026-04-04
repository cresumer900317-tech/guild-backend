from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import supabase
from scheduler import start_scheduler

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
    return result.data

@app.get("/api/members")
def get_members():
    result = supabase.table("members")\
        .select("*")\
        .execute()
    return result.data

@app.get("/api/weekly")
def get_weekly():
    result = supabase.table("members")\
        .select("*")\
        .order("weekly_diff", desc=True)\
        .execute()
    return result.data

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
    guilds = set(m["guild"] for m in members)
    avg_power = int(sum(m["power"] or 0 for m in members) / len(members))
    active = [m["server_rank"] for m in members if m.get("server_rank")]
    avg_rank = round(sum(active) / len(active), 1) if active else 0
    return {
        "guild_name": "친구패밀리",
        "guild_count": len(guilds),
        "member_count": len(members),
        "avg_power": avg_power,
        "avg_server_rank": avg_rank
    }

@app.post("/api/crawl")
def manual_crawl():
    from scheduler import run_crawl
    run_crawl()
    return {"status": "ok", "message": "크롤링 완료"}