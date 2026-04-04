from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from database import supabase
from fetch_mgf import fetch_mgf_data
from transform import transform_data
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def to_snake(members):
    result = []
    for m in members:
        result.append({
            "captured_at": m.get("capturedAt"),
            "guild": m.get("guild"),
            "name": m.get("name"),
            "job": m.get("job"),
            "level": m.get("level"),
            "power": m.get("power"),
            "power_text": m.get("powerText"),
            "guild_rank": m.get("guildRank"),
            "overall_rank": m.get("overallRank"),
            "server_rank": m.get("serverRank"),
            "server_rank_prev": m.get("serverRankPrev"),
            "server_rank_diff": m.get("serverRankDiff"),
            "server_rank_direction": m.get("serverRankDirection"),
            "weekly_diff": m.get("weeklyDiff"),
            "growth_rate": m.get("growthRate"),
            "popularity": m.get("popularity"),
            "detail_url": m.get("detailUrl"),
            "is_master": m.get("isMaster", False),
        })
    return result

def run_crawl():
    logger.info("=== 크롤링 시작 ===")
    try:
        raw_data = fetch_mgf_data()
        transformed = transform_data(raw_data)
        members = to_snake(transformed["members"])

        supabase.table("members").delete().neq("id", 0).execute()
        if members:
            supabase.table("members").insert(members).execute()
        logger.info(f"=== 크롤링 완료: {len(members)}명 저장 ===")
    except Exception as e:
        logger.error(f"크롤링 오류: {e}")

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_crawl, IntervalTrigger(hours=1))
    scheduler.start()
    logger.info("스케줄러 시작 (1시간마다 실행)")
    return scheduler