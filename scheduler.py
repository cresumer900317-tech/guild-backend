from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from database import supabase
from fetch_mgf import fetch_mgf_data
from transform import transform_data
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def rerank_by_guild(members):
    """길드별로 전투력 순 재정렬 후 guildRank 1부터 재부여"""
    from collections import defaultdict
    guild_groups = defaultdict(list)
    for m in members:
        guild_groups[m.get("guild", "")].append(m)

    result = []
    for guild, group in guild_groups.items():
        sorted_group = sorted(group, key=lambda x: x.get("power", 0) or 0, reverse=True)
        for idx, member in enumerate(sorted_group, start=1):
            member["guild_rank"] = idx
            result.append(member)
    return result


def to_snake(members):
    result = []
    for m in members:
        result.append({
            "captured_at": m.get("capturedAt"),
            "guild": m.get("guild"),
            "guild_level": m.get("guild_level", 0),
            "name": m.get("name"),
            "job": m.get("job"),
            "level": m.get("level"),
            "power": m.get("power"),
            "power_text": m.get("powerText") or m.get("power_text"),
            "guild_rank": m.get("guildRank"),
            "overall_rank": m.get("overallRank") or m.get("overall_rank"),
            "server_rank": m.get("serverRank") or m.get("server_rank"),
            "server_rank_prev": m.get("serverRankPrev") or m.get("server_rank_prev"),
            "server_rank_diff": m.get("serverRankDiff") or m.get("server_rank_diff"),
            "server_rank_direction": m.get("serverRankDirection") or m.get("server_rank_direction"),
            "weekly_diff": m.get("weeklyDiff") or m.get("weekly_diff"),
            "growth_rate": m.get("growthRate") or m.get("growth_rate"),
            "popularity": m.get("popularity"),
            "detail_url": m.get("detailUrl") or m.get("detail_url"),
            "is_master": m.get("isMaster") or m.get("is_master", False),
        })
    return result


def run_crawl():
    logger.info("=== 크롤링 시작 ===")
    try:
        raw_data = fetch_mgf_data()
        transformed = transform_data(raw_data)
        members_camel = transformed["members"]

        # 길드별 순위 재정렬
        members_camel = rerank_by_guild(members_camel)

        # snake_case 변환
        members = to_snake(members_camel)

        # 기존 데이터 삭제 후 새로 저장
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