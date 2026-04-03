from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import supabase

app = FastAPI()

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