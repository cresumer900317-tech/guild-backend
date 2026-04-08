from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from database import supabase
from fetch_mgf import fetch_mgf_data
from transform import transform_data
from datetime import datetime
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


def save_monthly_snapshot(members: list[dict]):
    """
    매달 1일 자정에 현재 멤버 데이터를 monthly_snapshots 테이블에 저장.
    snapshot_month = "YYYY-MM" (이번 달)
    이미 해당 월 스냅샷이 있으면 저장하지 않음 (월 1회만).
    """
    now = datetime.now()
    snapshot_month = now.strftime("%Y-%m")

    # 이미 이번 달 스냅샷이 있는지 확인
    existing = supabase.table("monthly_snapshots")\
        .select("id")\
        .eq("snapshot_month", snapshot_month)\
        .limit(1)\
        .execute()

    if existing.data:
        logger.info(f"[월간 스냅샷] {snapshot_month} 이미 존재 → 저장 건너뜀")
        return

    rows = []
    for m in members:
        rows.append({
            "snapshot_month": snapshot_month,
            "captured_at": now.isoformat(),
            "name": m.get("name"),
            "guild": m.get("guild"),
            "power": m.get("power"),
            "power_text": m.get("power_text"),
            "server_rank": m.get("server_rank"),
            "overall_rank": m.get("overall_rank"),
        })

    if rows:
        supabase.table("monthly_snapshots").upsert(
            rows,
            on_conflict="snapshot_month,name"
        ).execute()
        logger.info(f"[월간 스냅샷] {snapshot_month} 저장 완료: {len(rows)}명")


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
        return members

    except Exception as e:
        logger.error(f"크롤링 오류: {e}")
        return []


def save_rival_snapshot():
    """경쟁 길드 월간 스냅샷 저장 (매달 1일 실행)"""
    from datetime import datetime
    month = datetime.now().strftime("%Y-%m")
    logger.info(f"=== [경쟁 길드] 월간 스냅샷 저장: {month} ===")
    try:
        rival_names = ["싸이월드", "리안"]
        for name in rival_names:
            result = supabase.table("rival_guilds")                .select("total_power,member_count")                .eq("guild_name", name)                .order("captured_at", desc=True)                .limit(1)                .execute()
            if not result.data:
                continue
            latest = result.data[0]
            supabase.table("rival_snapshots").upsert({
                "snapshot_month": month,
                "guild_name": name,
                "total_power": latest["total_power"],
                "member_count": latest["member_count"],
            }, on_conflict="snapshot_month,guild_name").execute()
            logger.info(f"  [{name}] 스냅샷 저장: {latest['total_power']}")
    except Exception as e:
        logger.error(f"경쟁 길드 스냅샷 오류: {e}")


def run_rival_crawl():
    """경쟁 길드 데이터 수집 및 저장"""
    logger.info("=== [경쟁 길드] 크롤링 시작 ===")
    try:
        from fetch_mgf import fetch_rival_guilds
        summaries, members = fetch_rival_guilds()
        if summaries:
            supabase.table("rival_guilds").insert(summaries).execute()
            logger.info(f"[경쟁 길드] 요약 {len(summaries)}개 저장")
        if members:
            for guild_name in set(m["guild_name"] for m in members):
                supabase.table("rival_members")                    .delete()                    .eq("guild_name", guild_name)                    .execute()
            supabase.table("rival_members").insert(members).execute()
            logger.info(f"[경쟁 길드] 멤버 {len(members)}명 저장")
    except Exception as e:
        logger.error(f"경쟁 길드 크롤링 오류: {e}")


def run_crawl_and_snapshot():
    """크롤링 후 월간 스냅샷 저장 (매달 1일 자정 실행)"""
    logger.info("=== [월초] 크롤링 + 월간 스냅샷 저장 시작 ===")
    members = run_crawl()
    if members:
        save_monthly_snapshot(members)


def run_pop_rank_update():
    """인기도 서버 순위 크롤링 → DB 업데이트 (6시간마다)"""
    logger.info("=== [인기도 순위] 업데이트 시작 ===")
    try:
        from fetch_mgf import fetch_popularity_rank
        result = supabase.table("members").select("id, name").execute()
        members = result.data or []
        if not members:
            logger.info("[인기도 순위] 멤버 없음")
            return

        name_to_id = {m["name"]: m["id"] for m in members}
        rank_map = fetch_popularity_rank(set(name_to_id.keys()))

        updated = 0
        for name, pop_rank in rank_map.items():
            mid = name_to_id.get(name)
            if mid:
                supabase.table("members").update({"pop_server_rank": pop_rank}).eq("id", mid).execute()
                updated += 1

        # 미발견 멤버는 null 처리
        for name in (set(name_to_id.keys()) - set(rank_map.keys())):
            mid = name_to_id.get(name)
            if mid:
                supabase.table("members").update({"pop_server_rank": None}).eq("id", mid).execute()

        logger.info(f"=== [인기도 순위] 완료: {updated}명 갱신 ===")
    except Exception as e:
        logger.error(f"[인기도 순위] 오류: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler()

    # 1시간마다 일반 크롤링
    scheduler.add_job(run_crawl, IntervalTrigger(hours=1))

    # 1시간마다 경쟁 길드 크롤링
    scheduler.add_job(run_rival_crawl, IntervalTrigger(hours=1))

    # 1시간마다 인기도 서버 순위 업데이트
    scheduler.add_job(run_pop_rank_update, IntervalTrigger(hours=1))

    # 매달 1일 00:05에 크롤링 + 월간 스냅샷 저장
    scheduler.add_job(
        run_crawl_and_snapshot,
        CronTrigger(day=1, hour=0, minute=5)
    )

    # 매달 1일 00:10 경쟁 길드 스냅샷
    scheduler.add_job(
        save_rival_snapshot,
        CronTrigger(day=1, hour=0, minute=10)
    )

    scheduler.start()
    logger.info("스케줄러 시작 (1시간마다 크롤링, 매달 1일 00:05 스냅샷)")
    return scheduler