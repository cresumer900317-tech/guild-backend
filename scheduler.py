from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from database import supabase
from fetch_mgf import fetch_mgf_data
from transform import transform_data
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")  # Railway 컨테이너는 UTC — 날짜 경계 계산은 KST 명시


def _invalidate_cache(*keys):
    """크롤 성공 후 main의 응답 캐시 무효화 (TTL 만료 전 최신 데이터 노출).
    지연 import로 순환참조 회피. main 미로드(단독 실행) 시 무시."""
    try:
        from main import cache_clear
        cache_clear(*keys)
    except Exception:
        pass


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
    now = datetime.now(KST)
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
            "popularity": m.get("popularity"),
            "pop_server_rank": m.get("pop_server_rank"),
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

        # 별도 잡(인기도/보스 순위)이 채우는 컬럼 보존 — delete/insert 사이 유실 방지
        KEEP_COLS = ("pop_server_rank", "boss_score", "boss_rank", "wboss_score", "wboss_rank")
        existing = supabase.table("members").select("name," + ",".join(KEEP_COLS)).execute()
        keep_map = {m["name"]: {c: m.get(c) for c in KEEP_COLS} for m in (existing.data or [])}

        # 기존 데이터 삭제 후 새로 저장
        supabase.table("members").delete().neq("id", 0).execute()
        if members:
            for m in members:
                saved = keep_map.get(m.get("name")) or {}
                for c in KEEP_COLS:  # 모든 행에 동일 키 보장(이전 값 복원 or None)
                    m[c] = saved.get(c)
            supabase.table("members").insert(members).execute()

        logger.info(f"=== 크롤링 완료: {len(members)}명 저장 ===")
        _invalidate_cache("home_summary")
        return members

    except Exception as e:
        logger.error(f"크롤링 오류: {e}")
        return []


def save_rival_snapshot():
    """경쟁 길드 월간 스냅샷 저장 (매달 1일 실행)"""
    from datetime import datetime
    month = datetime.now(KST).strftime("%Y-%m")
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


def run_boss_rank_update():
    """토벌전/월드보스 점수·서버순위 크롤링 → members 테이블 업데이트 (1시간마다)"""
    logger.info("=== [보스 랭킹] 업데이트 시작 ===")
    try:
        from fetch_mgf import fetch_boss_ranking, norm_name
        result = supabase.table("members").select("id, name").execute()
        members = result.data or []
        if not members:
            logger.info("[보스 랭킹] 멤버 없음")
            return

        name_to_id = {m["name"]: m["id"] for m in members}
        names = set(name_to_id.keys())
        gb = fetch_boss_ranking(names, "guild_boss")   # 토벌전
        wb = fetch_boss_ranking(names, "world_boss")   # 월드보스

        updated = 0
        for raw_name, mid in name_to_id.items():
            n = norm_name(raw_name)
            patch = {
                "boss_score":  (gb.get(n) or {}).get("score"),
                "boss_rank":   (gb.get(n) or {}).get("rank"),
                "wboss_score": (wb.get(n) or {}).get("score"),
                "wboss_rank":  (wb.get(n) or {}).get("rank"),
            }
            supabase.table("members").update(patch).eq("id", mid).execute()
            if any(v is not None for v in patch.values()):
                updated += 1

        logger.info(f"=== [보스 랭킹] 완료: {updated}명 갱신 ===")
    except Exception as e:
        logger.error(f"[보스 랭킹] 오류: {e}")


def run_guild_rank_update():
    """친구 길드들의 서버 길드순위 크롤링 → guild_server_ranks upsert (1시간마다)"""
    logger.info("=== [길드 랭킹] 업데이트 시작 ===")
    try:
        from fetch_mgf import fetch_guild_server_ranks
        ranks = fetch_guild_server_ranks()
        if not ranks:
            logger.info("[길드 랭킹] 수집 결과 없음")
            return
        rows = [{
            "guild_name": gname,
            "server_rank": info["rank"],
            "guild_level": info["level"],
            "member_count": info["members"],
            "total_power": info["power"],
            "captured_at": datetime.now().isoformat(),
        } for gname, info in ranks.items()]
        supabase.table("guild_server_ranks").upsert(rows, on_conflict="guild_name").execute()
        logger.info(f"=== [길드 랭킹] 완료: {len(rows)}개 길드 ===")
    except Exception as e:
        logger.error(f"[길드 랭킹] 오류: {e}")


def run_server_guild_update():
    """스카니아11 서버 전체 길드 랭킹 Top-N 크롤 → server_guild_ranking 전량 교체.
    길드 랭킹 페이지는 가벼워(상위 30개=몇 페이지) 프록시 없이 직접 연결. 테이블 없으면 조용히 스킵."""
    logger.info("=== [서버 길드] 업데이트 시작 ===")
    try:
        import time as _time
        from fetch_mgf import fetch_server_guild_top, fetch_guild_member_powers
        rows = fetch_server_guild_top(limit=30, max_pages=12)
        if len(rows) < 3:
            logger.info(f"[서버 길드] 수집 {len(rows)}개뿐 → 교체 건너뜀(기존 유지)")
            return
        now = datetime.now().isoformat()
        # 각 길드 멤버 전투력 추가 수집 → 전력 균형(top/low/avg) 계산
        for r in rows:
            r["captured_at"] = now
            try:
                powers = fetch_guild_member_powers(r["guild_name"])
                if powers:
                    r["top_power"] = max(powers)
                    r["low_power"] = min(powers)
                    r["avg_member_power"] = sum(powers) // len(powers)
            except Exception as me:
                logger.warning(f"[서버 길드] {r.get('guild_name')} 멤버 전투력 수집 실패: {repr(me)[:80]}")
            _time.sleep(0.4)
        supabase.table("server_guild_ranking").delete().neq("guild_rank", 0).execute()
        supabase.table("server_guild_ranking").insert(rows).execute()
        _invalidate_cache("guild_health_*")
        logger.info(f"=== [서버 길드] 완료: {len(rows)}개 저장(균형 포함) ===")
    except Exception as e:
        logger.error(f"[서버 길드] 오류: {e}")


def run_server_boss_update():
    """스카니아11 서버 전체 토벌전·월드보스 랭킹 Top-N 크롤 → server_boss_ranking(kind별 교체).
    가벼워 프록시 불필요. 테이블 없으면 조용히 스킵."""
    logger.info("=== [서버 보스] 업데이트 시작 ===")
    try:
        from fetch_mgf import fetch_boss_top
        now = datetime.now().isoformat()
        for kind in ("guild_boss", "world_boss"):
            rows = fetch_boss_top(kind, limit=100, max_pages=60)
            if len(rows) < 3:
                logger.info(f"[서버 보스] {kind} {len(rows)}명뿐 → 교체 건너뜀")
                continue
            for r in rows:
                r["kind"] = kind
                r["captured_at"] = now
            supabase.table("server_boss_ranking").delete().eq("kind", kind).execute()
            CHUNK = 100
            for i in range(0, len(rows), CHUNK):
                supabase.table("server_boss_ranking").insert(rows[i:i + CHUNK]).execute()
            logger.info(f"=== [서버 보스] {kind} {len(rows)}명 저장 ===")
    except Exception as e:
        logger.error(f"[서버 보스] 오류: {e}")


def run_server_top_update():
    """스카니아11 서버 전체 랭킹 Top-N 크롤 → server_ranking 테이블 전량 교체 (하루 2회)"""
    logger.info("=== [서버 전체] 업데이트 시작 ===")
    try:
        from fetch_mgf import fetch_server_top
        rows = fetch_server_top(limit=7000, max_pages=240)
        # 크롤 실패(부분 수집) 시 기존 데이터 보존 — 빈/반쪽 교체 방지
        if len(rows) < 100:
            logger.info(f"[서버 전체] 수집 {len(rows)}명뿐 → 교체 건너뜀(기존 유지)")
            return
        # 데이터센터 IP(Railway)는 mgf rate-limit으로 ~960에서 끊김. 이미 더 큰 데이터가
        # 있으면(거주지 IP 풀크롤로 채운 경우) 부분수집으로 깎지 않는다.
        try:
            existing_count = supabase.table("server_ranking").select("server_rank", count="exact").limit(1).execute().count or 0
        except Exception:
            existing_count = 0
        if existing_count >= 1000 and len(rows) < existing_count * 0.8:
            logger.info(f"[서버 전체] 수집 {len(rows)}명 < 기존 {existing_count}×0.8 → 차단 의심, 교체 건너뜀(기존 유지)")
            return
        now = datetime.now().isoformat()
        for r in rows:
            r["captured_at"] = now
        # 전량 교체 (server_rank PK)
        supabase.table("server_ranking").delete().neq("server_rank", 0).execute()
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            supabase.table("server_ranking").insert(rows[i:i + CHUNK]).execute()
        _invalidate_cache("server_ranking_rows", "home_summary", "guild_health_*")
        logger.info(f"=== [서버 전체] 완료: {len(rows)}명 저장 ===")

        # 일별 이력 적립(프로필 성장 그래프용). 테이블(server_ranking_history) 없으면 조용히 스킵.
        try:
            today = datetime.now(KST).strftime("%Y-%m-%d")
            hist = [{
                "snapshot_date": today,
                "name": r.get("nickname"),
                "server_rank": r.get("server_rank"),
                "guild": r.get("guild"),
                "power": r.get("power"),
                "popularity": r.get("popularity"),
            } for r in rows]
            for i in range(0, len(hist), CHUNK):
                supabase.table("server_ranking_history").upsert(
                    hist[i:i + CHUNK], on_conflict="snapshot_date,name"
                ).execute()
            logger.info(f"[서버 이력] {today} {len(hist)}명 적립")
        except Exception as he:
            logger.warning(f"[서버 이력] 적립 스킵(테이블 미생성?): {repr(he)[:120]}")
    except Exception as e:
        logger.error(f"[서버 전체] 오류: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler()

    # IntervalTrigger는 첫 실행이 "시작 +1시간"이라, 재배포가 잦으면 그 1시간 안에
    # 컨테이너가 리셋되어 크롤이 한 번도 안 도는 경우가 생긴다(토벌전·월드보스가 계속 null이던 원인).
    # → 앱 데이터 핵심 크롤 4종은 next_run_time=now로 시작 직후 1회 즉시 실행해 항상 채워지게 한다.
    #   (BackgroundScheduler 워커 스레드에서 돌아 웹서버 부팅을 막지 않음)
    now = datetime.now()

    # 1시간마다 일반 크롤링 (전투력/멤버) — 시작 시 즉시 1회
    scheduler.add_job(run_crawl, IntervalTrigger(hours=1), next_run_time=now)

    # 1시간마다 경쟁 길드 크롤링
    scheduler.add_job(run_rival_crawl, IntervalTrigger(hours=1))

    # 1시간마다 인기도 서버 순위 업데이트 — 시작 시 즉시 1회
    scheduler.add_job(run_pop_rank_update, IntervalTrigger(hours=1), next_run_time=now)

    # 1시간마다 토벌전/월드보스 순위 + 길드 서버순위 업데이트 — 시작 시 즉시 1회
    scheduler.add_job(run_boss_rank_update, IntervalTrigger(hours=1), next_run_time=now)
    scheduler.add_job(run_guild_rank_update, IntervalTrigger(hours=1), next_run_time=now)

    # 6시간마다 스카니아11 서버 전체 길드 랭킹 Top30 — 시작 직후 1회 (가벼워 프록시 불필요)
    scheduler.add_job(run_server_guild_update, IntervalTrigger(hours=6), next_run_time=now)

    # 6시간마다 스카니아11 서버 전체 보스(토벌전·월드보스) Top100 — 시작 직후 1회
    scheduler.add_job(run_server_boss_update, IntervalTrigger(hours=6), next_run_time=now)

    # 서버 전체 랭킹(~6800명)은 무겁고 mgf 부담을 줄이려 하루 2회(12h) + 시작 직후 1회.
    # PROXY_URL 미설정 시 Railway IP는 ~960에서 막혀 가드가 교체를 스킵(기존 데이터 보존).
    scheduler.add_job(
        run_server_top_update,
        IntervalTrigger(hours=12),
        next_run_time=datetime.now(),
    )

    # 매달 1일 00:05 KST에 크롤링 + 월간 스냅샷 저장 (컨테이너=UTC라 timezone 명시)
    scheduler.add_job(
        run_crawl_and_snapshot,
        CronTrigger(day=1, hour=0, minute=5, timezone="Asia/Seoul")
    )

    # 매달 1일 00:10 KST 경쟁 길드 스냅샷
    scheduler.add_job(
        save_rival_snapshot,
        CronTrigger(day=1, hour=0, minute=10, timezone="Asia/Seoul")
    )

    # 매일 08:00 KST 개인 업무 디지스트 이메일
    try:
        from email_digest import run_daily_digest
        scheduler.add_job(
            run_daily_digest,
            CronTrigger(hour=8, minute=0, timezone="Asia/Seoul")
        )
        logger.info("디지스트 잡 등록 완료 (매일 08:00 KST)")
    except Exception as e:
        logger.error(f"디지스트 잡 등록 실패: {e}")

    # 5분마다 일정 푸시 (시작/마지막날/마감3h·1h, 중복은 push_log로 방지)
    try:
        from push_send import run_schedule_push
        scheduler.add_job(run_schedule_push, IntervalTrigger(minutes=5))
        logger.info("일정 푸시 잡 등록 완료 (5분 간격)")
    except Exception as e:
        logger.error(f"일정 푸시 잡 등록 실패: {e}")

    scheduler.start()
    logger.info("스케줄러 시작 (1시간마다 크롤링, 매달 1일 00:05 스냅샷, 매일 08:00 KST 디지스트, 5분마다 일정푸시)")
    return scheduler