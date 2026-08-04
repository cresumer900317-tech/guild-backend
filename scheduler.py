from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from database import supabase
from fetch_mgf import fetch_mgf_data
from transform import transform_data
from datetime import datetime, timedelta
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


def _record_changes(old_rows, new_rows, name_key="name"):
    """크롤 diff → change_log에 길드/직업 변경 기록. 레벨·전투력 추이는 server_ranking_history 그래프 담당."""
    try:
        from main import supabase
        old = {r.get(name_key): r for r in (old_rows or []) if r.get(name_key)}
        new = {r.get(name_key): r for r in (new_rows or []) if r.get(name_key)}
        if not old or not new:
            return
        logs = []
        for name, n in new.items():
            o = old.get(name)
            if not o:
                continue
            for field in ("guild", "job"):
                ov = str(o.get(field) or "").strip()
                nv = str(n.get(field) or "").strip()
                if ov and nv and ov != nv:
                    logs.append({"name": name, "field": field, "old_value": ov, "new_value": nv})
        if logs:
            supabase.table("change_log").insert(logs).execute()
            logger.info(f"[변경 이력] {len(logs)}건 기록")
    except Exception as e:
        logger.warning(f"[변경 이력] 기록 실패: {repr(e)[:100]}")


def _detect_rename_suspects(old_rows, new_rows):
    """길드원 크롤 diff에서 닉변 의심 감지 — 사라진 이름↔새 이름의 직업 일치 + 레벨±3 + 전투력±10%가
    유일 후보일 때만. 확정은 admin에서 사람이 한다."""
    try:
        from main import supabase
        old = {r["name"]: r for r in (old_rows or []) if r.get("name")}
        new = {r["name"]: r for r in (new_rows or []) if r.get("name")}
        gone = [o for k, o in old.items() if k not in new]
        appeared = [n for k, n in new.items() if k not in old]
        if not gone or not appeared:
            return
        suspects = []
        for o in gone:
            op = int(o.get("power") or 0)
            cands = [
                n for n in appeared
                if str(n.get("job") or "") == str(o.get("job") or "")
                and abs(int(n.get("level") or 0) - int(o.get("level") or 0)) <= 3
                and (op == 0 or abs(int(n.get("power") or 0) - op) <= max(int(op * 0.1), 1))
            ]
            if len(cands) == 1:
                c = cands[0]
                suspects.append({
                    "old_name": o["name"], "new_name": c["name"],
                    "evidence": f"직업 {o.get('job')} 일치 · Lv {o.get('level')}→{c.get('level')} · 전투력 {op:,}→{int(c.get('power') or 0):,}",
                })
        if not suspects:
            return
        supabase.table("rename_suspects").upsert(
            suspects, on_conflict="old_name,new_name", ignore_duplicates=True
        ).execute()
        logger.info(f"[닉변 감지] 의심 {len(suspects)}건: " + ", ".join(f"{s['old_name']}→{s['new_name']}" for s in suspects))
        try:
            from push_send import notify_admins
            lines = ", ".join(f"{s['old_name']}→{s['new_name']}" for s in suspects[:3])
            notify_admins("🔤 닉변 의심 감지", f"{lines} — admin에서 확인해주세요.")
        except Exception as e:
            logger.warning(f"[닉변 감지] 푸시 실패: {e}")
    except Exception as e:
        logger.warning(f"[닉변 감지] 실패: {repr(e)[:100]}")


def _warm_home_caches():
    """홈 API 응답 캐시 예열 — 무효화 직후 재계산 비용(guild-health 최대 ~6s)을 방문자 대신 미리 부담.
    캐시가 살아있는 키는 즉시 반환되므로 반복 호출 부담 없음. 지연 import로 순환참조 회피."""
    try:
        import main
        main.load_server_ranking_rows()
        main.get_monthly()
        main.get_guild_ranks()
        main.get_server_guild_ranking(30)
        main.get_server_stats()
        main.get_guild_health(30)
        main.get_home_summary()
    except Exception as e:
        logger.warning(f"[캐시 예열] {repr(e)[:120]}")


# 크롤 잡별 연속 실패 카운터 — 임계 도달 시 운영진 푸시 1회(성공하면 리셋).
# 지금까지는 로그로만 남아 실패가 조용히 묻혔다(토벌전 계속 null이던 류).
_fail_counts = {}
_FAIL_ALERT_AT = {"크롤링": 3, "서버 전체": 4}   # 1h 간격 3회 / 12h 간격 4회(=2일)


def _track_job(job: str, ok: bool, detail: str = ""):
    if ok:
        _fail_counts.pop(job, None)
        return
    n = _fail_counts.get(job, 0) + 1
    _fail_counts[job] = n
    threshold = _FAIL_ALERT_AT.get(job, 3)
    if n == threshold:   # 임계 도달 순간에만 1회 발송(스팸 방지)
        try:
            from push_send import notify_admins
            notify_admins(f"⚠️ [{job}] 크롤 연속 {n}회 실패",
                          (detail or "데이터 갱신이 멈췄을 수 있어요.")[:120] + " Railway 로그를 확인해주세요.")
        except Exception as e:
            logger.error(f"[{job}] 실패 알림 발송 불가: {e}")


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
        # guild/job/level/power는 변경 이력 diff·닉변 감지용으로 같이 읽는다
        KEEP_COLS = ("pop_server_rank", "boss_score", "boss_rank", "wboss_score", "wboss_rank")
        existing = supabase.table("members").select("name,guild,job,level,power," + ",".join(KEEP_COLS)).execute()
        keep_map = {m["name"]: {c: m.get(c) for c in KEEP_COLS} for m in (existing.data or [])}

        # 새 데이터 insert 후 이전 행 삭제 — delete→insert 사이 API가 0명으로 응답하던 빈 창 제거.
        # insert 반환 id에 의존하지 않는다(반환이 비면 이전 배치가 남아 전원 2배가 됨, 2026-08-03 장애).
        # 이번 배치 captured_at 최솟값 미만 = 이전 크롤 행.
        if members:
            for m in members:
                saved = keep_map.get(m.get("name")) or {}
                for c in KEEP_COLS:  # 모든 행에 동일 키 보장(이전 값 복원 or None)
                    m[c] = saved.get(c)
            supabase.table("members").insert(members).execute()
            batch_ts = [m["captured_at"] for m in members if m.get("captured_at")]
            if batch_ts:
                supabase.table("members").delete().lt("captured_at", min(batch_ts)).execute()
            else:
                logger.warning("[크롤링] captured_at 없음 — 이전 행 정리 건너뜀")

        logger.info(f"=== 크롤링 완료: {len(members)}명 저장 ===")
        _record_changes(existing.data, members)
        _detect_rename_suspects(existing.data, members)
        _invalidate_cache("home_summary", "monthly", "weekly_growth", "growth_story")
        _warm_home_caches()
        _track_job("크롤링", ok=bool(members), detail="mgf.gg 멤버 크롤 결과가 비어있어요.")
        return members

    except Exception as e:
        logger.error(f"크롤링 오류: {e}")
        _track_job("크롤링", ok=False, detail=repr(e)[:100])
        return []


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
        _invalidate_cache("guild_ranks")
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
        _invalidate_cache("guild_health_*", "server_guild_ranking_*")
        _warm_home_caches()
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
            _track_job("서버 전체", ok=False, detail=f"수집 {len(rows)}명뿐(차단 의심). 이력도 안 쌓이는 중.")
            return
        # 데이터센터 IP(Railway)는 mgf rate-limit으로 ~960에서 끊김. 이미 더 큰 데이터가
        # 있으면(거주지 IP 풀크롤로 채운 경우) 부분수집으로 깎지 않는다.
        try:
            existing_count = supabase.table("server_ranking").select("server_rank", count="exact").limit(1).execute().count or 0
        except Exception:
            existing_count = 0
        if existing_count >= 1000 and len(rows) < existing_count * 0.8:
            logger.info(f"[서버 전체] 수집 {len(rows)}명 < 기존 {existing_count}×0.8 → 차단 의심, 교체 건너뜀(기존 유지)")
            _track_job("서버 전체", ok=False, detail=f"수집 {len(rows)}/{existing_count}명(차단 의심). 이력도 안 쌓이는 중.")
            return
        # 교체 전 이전 데이터 확보 — 서버 전체 변경 이력(길드/직업) diff용
        try:
            from main import load_server_ranking_rows
            prev_rows = load_server_ranking_rows()
        except Exception:
            prev_rows = []
        now = datetime.now().isoformat()
        for r in rows:
            r["captured_at"] = now
        # 전량 교체 (server_rank PK)
        supabase.table("server_ranking").delete().neq("server_rank", 0).execute()
        CHUNK = 500
        for i in range(0, len(rows), CHUNK):
            supabase.table("server_ranking").insert(rows[i:i + CHUNK]).execute()
        _invalidate_cache("server_ranking_rows", "home_summary", "guild_health_*", "server_stats")
        _warm_home_caches()
        _record_changes(prev_rows, rows, name_key="nickname")
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
        _track_job("서버 전체", ok=True)
    except Exception as e:
        logger.error(f"[서버 전체] 오류: {e}")
        _track_job("서버 전체", ok=False, detail=repr(e)[:100])


def start_scheduler():
    scheduler = BackgroundScheduler()

    # IntervalTrigger는 첫 실행이 "시작 +1시간"이라, 재배포가 잦으면 그 1시간 안에
    # 컨테이너가 리셋되어 크롤이 한 번도 안 도는 경우가 생긴다(토벌전·월드보스가 계속 null이던 원인).
    # → 앱 데이터 핵심 크롤 4종은 next_run_time=now로 시작 직후 1회 즉시 실행해 항상 채워지게 한다.
    #   (BackgroundScheduler 워커 스레드에서 돌아 웹서버 부팅을 막지 않음)
    now = datetime.now()

    # 1시간마다 일반 크롤링 (전투력/멤버) — 시작 시 즉시 1회
    scheduler.add_job(run_crawl, IntervalTrigger(hours=1), next_run_time=now)

    # 1시간마다 인기도 서버 순위 업데이트 — 시작 시 즉시 1회
    scheduler.add_job(run_pop_rank_update, IntervalTrigger(hours=1), next_run_time=now)

    # 1시간마다 토벌전/월드보스 순위 + 길드 서버순위 업데이트 — 시작 시 즉시 1회
    scheduler.add_job(run_boss_rank_update, IntervalTrigger(hours=1), next_run_time=now)
    scheduler.add_job(run_guild_rank_update, IntervalTrigger(hours=1), next_run_time=now)

    # 6시간마다 스카니아11 서버 전체 길드 랭킹 Top30 — 시작 직후 1회 (가벼워 프록시 불필요)
    scheduler.add_job(run_server_guild_update, IntervalTrigger(hours=6), next_run_time=now)

    # 10분마다 홈 캐시 예열 — 재기동/수동 무효화 등으로 식은 캐시를 방문자가 아닌 서버가 데운다
    scheduler.add_job(_warm_home_caches, IntervalTrigger(minutes=10), next_run_time=now + timedelta(minutes=2))

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