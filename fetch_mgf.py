import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from config import (
    TARGET_GUILD_URLS,
    REQUEST_TIMEOUT,
    REQUEST_DELAY_SECONDS,
    DETAIL_REQUEST_DELAY_SECONDS,
    DETAIL_REQUEST_RETRIES,
    USER_AGENT,
    MAX_MEMBERS_PER_GUILD,
    EXCLUDED_MEMBER_NAMES,
    FORCE_INCLUDE_MEMBERS,
    LATEST_SNAPSHOT_PATH,
    PREVIOUS_SNAPSHOT_PATH,
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://mgf.gg/",
}

_session = requests.Session()
_session.headers.update(HEADERS)

def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name)

def save_debug_html(guild_name: str, html: str) -> None:
    filename = Path(__file__).resolve().parent / f"debug_guild_{safe_filename(guild_name)}.html"
    filename.write_text(html, encoding="utf-8")

def fetch_page(url: str, retries: int = 1) -> str:
    last_error = None
    for _ in range(retries + 1):
        try:
            res = _session.get(url, timeout=REQUEST_TIMEOUT)
            res.raise_for_status()
            res.encoding = "utf-8"
            return res.text
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise last_error

def convert_korean_power_to_int(text: str) -> int:
    text = str(text).replace(",", "").replace(" ", "")
    total = 0
    for unit, multiplier in [("경", 10**16), ("조", 10**12), ("억", 10**8), ("만", 10**4)]:
        m = re.search(rf"(\d+){unit}", text)
        if m:
            total += int(m.group(1)) * multiplier
    tail = re.sub(r"\d+(경|조|억|만)", "", text)
    if tail.isdigit():
        total += int(tail)
    return total

def parse_number(text: str) -> int:
    cleaned = re.sub(r"[^0-9]", "", str(text))
    return int(cleaned) if cleaned else 0

def parse_guild_level(html: str) -> int:
    """길드 페이지 HTML에서 길드 레벨 파싱"""
    soup = BeautifulSoup(html, "html.parser")

    # stat-pill-label이 "레벨"인 pill의 value 찾기
    for pill in soup.select(".stat-pill"):
        label = pill.select_one(".stat-pill-label")
        value = pill.select_one(".stat-pill-value")
        if label and value and "레벨" in label.get_text():
            m = re.search(r"(\d+)", value.get_text())
            if m:
                return int(m.group(1))
    return 0

def parse_detail_page(detail_url: str) -> dict:
    default = {"overall_rank": 0, "server_rank": 0, "popularity": 0}
    if not detail_url:
        return default
    try:
        html = fetch_page(detail_url, retries=DETAIL_REQUEST_RETRIES)
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text("\n", strip=True)
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]

        overall_rank = 0
        server_rank = 0
        popularity = 0

        for idx, line in enumerate(lines):
            if "♥" in line and popularity == 0:
                popularity = parse_number(line)
            if "전체 랭킹" in line and idx + 1 < len(lines):
                overall_rank = parse_number(lines[idx + 1])
            if "Scania" in line and idx + 1 < len(lines):
                candidate = parse_number(lines[idx + 1])
                if candidate:
                    server_rank = candidate

        if not overall_rank:
            m = re.search(r"전체\s*랭킹\s*(\d+)위", page_text)
            if m:
                overall_rank = int(m.group(1))
        if not server_rank:
            m = re.search(r"Scania\s*\d+\s*(\d+)위", page_text)
            if m:
                server_rank = int(m.group(1))
        if not popularity:
            m = re.search(r"♥\s*(\d+)", page_text)
            if m:
                popularity = int(m.group(1))

        return {"overall_rank": overall_rank, "server_rank": server_rank, "popularity": popularity}
    except Exception:
        return default

def parse_members_from_html(html: str, guild_name: str, guild_level: int = 0):
    soup = BeautifulSoup(html, "html.parser")
    members = []
    rows = soup.select(".members-list .member-row")
    for idx, row in enumerate(rows, start=1):
        name_el = row.select_one(".nick-link")
        sub_el = row.select_one(".member-sub")
        power_el = row.select_one(".power-tooltip") or row.select_one(".power-text")
        rank_el = row.select_one(".member-rank")
        detail_el = row.select_one(".detail-btn")
        master_el = row.select_one(".inline-master")
        if not name_el or not sub_el or not power_el:
            continue
        name = name_el.get_text(" ", strip=True)
        if name in EXCLUDED_MEMBER_NAMES:
            print(f"[EXCLUDE] {guild_name} / {name}")
            continue
        sub_text = sub_el.get_text(" ", strip=True)
        level_match = re.search(r"Lv\.?\s*(\d+)", sub_text)
        level = int(level_match.group(1)) if level_match else 0
        job = re.sub(r"\s*\|\s*Lv\.?\s*\d+", "", sub_text).strip()
        job = re.sub(r"Lv\.?\s*\d+", "", job).strip()
        power_text = power_el.get_text(" ", strip=True)
        guild_rank_text = rank_el.get_text(strip=True) if rank_el else str(idx)
        guild_rank = int(re.sub(r"[^0-9]", "", guild_rank_text) or idx)
        detail_url = detail_el.get("href", "") if detail_el else ""
        if detail_url.startswith("/"):
            detail_url = "https://mgf.gg" + detail_url
        detail = parse_detail_page(detail_url)
        time.sleep(DETAIL_REQUEST_DELAY_SECONDS)
        members.append({
            "capturedAt": datetime.now().isoformat(timespec="seconds"),
            "guild": guild_name,
            "guild_level": guild_level,
            "guildRank": guild_rank,
            "name": name,
            "job": job or "미확인",
            "level": level,
            "power": convert_korean_power_to_int(power_text),
            "power_text": power_text,
            "detail_url": detail_url,
            "image": "",
            "is_master": bool(master_el),
            "overall_rank": detail["overall_rank"],
            "server_rank": detail["server_rank"],
            "popularity": detail["popularity"],
        })
    members = sorted(members, key=lambda x: x.get("guildRank", 9999))
    return members[:MAX_MEMBERS_PER_GUILD]

def clean_guild_name(text: str) -> str:
    """길드명에서 이모지, 특수문자 제거하고 순수 텍스트만 반환."""
    cleaned = re.sub(r"[^\w가-힣a-zA-Z0-9]", "", text).strip()
    return cleaned

def verify_member_guild(name: str) -> str | None:
    """개별 캐릭터 페이지에서 현재 길드명을 확인. 실패 시 None 반환."""
    url = f"https://mgf.gg/contents/character.php?n={requests.utils.quote(name)}"
    try:
        html = fetch_page(url, retries=2)
        soup = BeautifulSoup(html, "html.parser")
        guild_links = soup.select("a[href*='guild_info.php']")
        for link in guild_links:
            raw = link.get_text(strip=True)
            cleaned = clean_guild_name(raw)
            if cleaned:
                return cleaned
        return None
    except Exception:
        return None

def recover_missing_members(current_members: list[dict], guild_levels: dict[str, int]):
    """이전 스냅샷에 있었지만 이번 수집에서 누락된 멤버를 개별 확인 후 복구."""
    prev_snapshot = load_snapshot(PREVIOUS_SNAPSHOT_PATH)
    if not prev_snapshot:
        prev_snapshot = load_snapshot(LATEST_SNAPSHOT_PATH)
    prev_rows = prev_snapshot.get("rows", []) if isinstance(prev_snapshot, dict) else []
    if not prev_rows:
        return []

    current_names = {m["name"] for m in current_members}
    prev_map = {row["name"]: row for row in prev_rows if row.get("name")}
    missing = [row for name, row in prev_map.items()
               if name not in current_names
               and name not in EXCLUDED_MEMBER_NAMES
               and row.get("guild") in TARGET_GUILD_URLS]

    if not missing:
        return []

    print(f"[보완 수집] 이전 대비 누락 {len(missing)}명 감지 → 개별 확인 시작")
    recovered = []
    for prev_row in missing:
        name = prev_row["name"]
        expected_guild = prev_row.get("guild", "")
        actual_guild = verify_member_guild(name)
        time.sleep(DETAIL_REQUEST_DELAY_SECONDS)

        if actual_guild and actual_guild in TARGET_GUILD_URLS:
            print(f"  [복구] {name}: {expected_guild} → {actual_guild} (유지)")
            detail_url = f"https://mgf.gg/contents/character.php?n={requests.utils.quote(name)}"
            detail = parse_detail_page(detail_url)
            time.sleep(DETAIL_REQUEST_DELAY_SECONDS)
            recovered.append({
                "capturedAt": datetime.now().isoformat(timespec="seconds"),
                "guild": actual_guild,
                "guild_level": guild_levels.get(actual_guild, prev_row.get("guild_level", 0)),
                "guildRank": prev_row.get("guildRank", 99),
                "name": name,
                "job": prev_row.get("job", "미확인"),
                "level": prev_row.get("level", 0),
                "power": prev_row.get("power", 0),
                "power_text": prev_row.get("power_text", ""),
                "detail_url": detail_url,
                "image": "",
                "is_master": prev_row.get("is_master", False),
                "overall_rank": detail["overall_rank"],
                "server_rank": detail["server_rank"],
                "popularity": detail["popularity"],
            })
        else:
            print(f"  [제거] {name}: 길드 변경 또는 탈퇴 ({actual_guild})")

    print(f"[보완 수집] 복구 {len(recovered)}명")
    return recovered

def fetch_mgf_data():
    print("=== MGF 길드 수집 시작 ===")
    all_members = []
    guild_levels = {}
    for guild_name, url in TARGET_GUILD_URLS.items():
        print(f"수집 중: {guild_name}")
        html = fetch_page(url, retries=1)
        save_debug_html(guild_name, html)
        guild_level = parse_guild_level(html)
        guild_levels[guild_name] = guild_level
        print(f" -> 길드 레벨: {guild_level}")
        members = parse_members_from_html(html, guild_name, guild_level)
        print(f" -> {len(members)}명")
        all_members.extend(members)
        time.sleep(REQUEST_DELAY_SECONDS)

    # 누락 멤버 보완
    recovered = recover_missing_members(all_members, guild_levels)
    all_members.extend(recovered)

    # 강제 포함 멤버 수집
    current_names = {m["name"] for m in all_members}
    for force_name in FORCE_INCLUDE_MEMBERS:
        if force_name in current_names or force_name in EXCLUDED_MEMBER_NAMES:
            continue
        guild = verify_member_guild(force_name)
        if guild and guild in TARGET_GUILD_URLS:
            print(f"[강제 수집] {force_name} → {guild}")
            detail_url = f"https://mgf.gg/contents/character.php?n={requests.utils.quote(force_name)}"
            html = fetch_page(detail_url, retries=2)
            soup = BeautifulSoup(html, "html.parser")
            page_text = soup.get_text("\n", strip=True)
            # 전투력 파싱
            power_text = ""
            power = 0
            power_el = soup.select_one(".power-tooltip") or soup.select_one(".power-text")
            if power_el:
                power_text = power_el.get_text(" ", strip=True)
                power = convert_korean_power_to_int(power_text)
            # 레벨/직업
            level = 0
            job = "미확인"
            lv_match = re.search(r"Lv\.?\s*(\d+)", page_text)
            if lv_match:
                level = int(lv_match.group(1))
            detail = parse_detail_page(detail_url)
            time.sleep(DETAIL_REQUEST_DELAY_SECONDS)
            all_members.append({
                "capturedAt": datetime.now().isoformat(timespec="seconds"),
                "guild": guild,
                "guild_level": guild_levels.get(guild, 0),
                "guildRank": 99,
                "name": force_name,
                "job": job,
                "level": level,
                "power": power,
                "power_text": power_text,
                "detail_url": detail_url,
                "image": "",
                "is_master": False,
                "overall_rank": detail["overall_rank"],
                "server_rank": detail["server_rank"],
                "popularity": detail["popularity"],
            })
        else:
            print(f"[강제 수집] {force_name}: 우리 길드 아님 ({guild})")

    print(f"총 수집 인원: {len(all_members)}명")
    return all_members

def load_snapshot(path: Path) -> dict:
    if not path.exists():
        return {"capturedAt": None, "rows": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"capturedAt": None, "rows": []}

def save_snapshot(latest_rows: list[dict]):
    latest = load_snapshot(LATEST_SNAPSHOT_PATH)
    previous_rows = latest.get("rows") if isinstance(latest, dict) else []
    if previous_rows:
        PREVIOUS_SNAPSHOT_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot = {"capturedAt": datetime.now().isoformat(timespec="seconds"), "rows": latest_rows}
    LATEST_SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

# ── 경쟁 길드 크롤링 ────────────────────────────────────────
RIVAL_GUILDS = {
    "싸이월드": "https://mgf.gg/contents/guild_info.php?g_name=%EC%8B%B8%EC%9D%B4%EC%9B%94%EB%93%9C",
    "리안":     "https://mgf.gg/contents/guild_info.php?g_name=%EB%A6%AC%EC%95%88",
}

def parse_rival_guild(html: str, guild_name: str) -> tuple[dict, list[dict]]:
    """경쟁 길드 HTML 파싱 - power-tooltip 역방향 탐색 방식 (tr 없는 환경 대응)"""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    now = datetime.now().isoformat(timespec="seconds")

    # 서버/전체 순위
    server_rank = 0
    overall_rank = 0
    for i, line in enumerate(lines):
        if "서버 순위" in line and i + 1 < len(lines):
            server_rank = parse_number(lines[i + 1])
        if "전체 순위" in line and i + 1 < len(lines):
            overall_rank = parse_number(lines[i + 1])

    guild_level = parse_guild_level(html)

    # power-tooltip 목록: [0]=길드 총전투력, [1:]=멤버별
    power_tips = soup.select(".power-tooltip")
    member_power_tips = power_tips[1:] if len(power_tips) > 1 else power_tips

    members = []
    for i, pt in enumerate(member_power_tips):
        pw = convert_korean_power_to_int(pt.get_text(strip=True))
        if pw <= 0:
            continue

        # 이름: pt 이전의 title 있는 character.php 링크
        name = ""
        tr_elem = pt  # 참조용
        prev_a = pt.find_previous("a", href=re.compile(r"character\.php"))
        if prev_a:
            name = prev_a.get("title") or prev_a.get_text(strip=True)
        # 🔍 아이콘이면 한 단계 더 앞으로
        if name in ["🔍", ""]:
            prev_a = prev_a.find_previous("a", href=re.compile(r"character\.php")) if prev_a else None
            if prev_a:
                name = prev_a.get("title") or prev_a.get_text(strip=True)
        if not name:
            continue

        # 직업: pt 이전의 companion_jobs img
        job = ""
        prev_img = pt.find_previous("img", src=re.compile(r"companion_jobs"))
        if prev_img:
            m = re.search(r"companion_jobs/(.+)\.png", prev_img.get("src", ""))
            if m:
                job = m.group(1)

        # 레벨: pt 이전 텍스트에서 Lv.N
        level = 0
        prev_text = pt.find_previous(string=re.compile(r"Lv\.?\d+"))
        if prev_text:
            lm = re.search(r"Lv\.?(\d+)", str(prev_text))
            if lm:
                level = int(lm.group(1))

        # 캐릭터 상세 URL
        detail_url = f"https://mgf.gg/contents/character.php?n={requests.utils.quote(name)}"

        # 인기도: 캐릭터 상세 페이지에서 파싱
        popularity = 0
        try:
            detail_html = fetch_page(detail_url, retries=DETAIL_REQUEST_RETRIES)
            detail_info = parse_detail_page(detail_url)
            popularity = detail_info.get("popularity", 0)
            time.sleep(DETAIL_REQUEST_DELAY_SECONDS)
        except Exception:
            pass

        members.append({
            "captured_at": now,
            "guild_name": guild_name,
            "name": name,
            "job": job,
            "level": level,
            "power": pw,
            "power_text": pt.get_text(strip=True),
            "guild_rank": len(members) + 1,
            "popularity": popularity,
        })

    # 길드 요약
    total_power = sum(m["power"] for m in members)
    member_count = len(members)
    top1 = members[0] if members else {}
    avg_level = round(sum(m["level"] for m in members) / member_count, 1) if member_count else 0

    guild_summary = {
        "guild_name": guild_name,
        "captured_at": now,
        "total_power": total_power,
        "member_count": member_count,
        "server_rank": server_rank,
        "overall_rank": overall_rank,
        "guild_level": guild_level,
        "top1_name": top1.get("name", ""),
        "top1_power": top1.get("power", 0),
        "top1_job": top1.get("job", ""),
    }

    print(f"  [파싱] {guild_name}: {member_count}명, 총전투력 {total_power}")
    return guild_summary, members


# ── 인기도 서버 순위 크롤링 ────────────────────────────────
POP_RANK_URL = "https://mgf.gg/ranking/pop.php?server=11&recent=1&stx=&page={page}"
FRIEND_GUILDS = {"친구들", "친구둘", "친구삼", "친구넷", "친구닷"}

def fetch_popularity_rank(member_names: set[str], max_pages: int = 100) -> dict[str, int]:
    """
    mgf.gg 스카니아11 인기도 랭킹 페이지를 순회하며
    친구패밀리 멤버의 서버 인기도 순위를 반환.
    반환값: { 닉네임: 서버_인기도_순위 }
    멤버 전원 발견 또는 max_pages 도달 시 조기 종료.
    """
    found: dict[str, int] = {}
    remaining = set(member_names)
    empty_streak = 0

    for page in range(1, max_pages + 1):
        if not remaining:
            break
        url = POP_RANK_URL.format(page=page)
        try:
            html = fetch_page(url, retries=2)
        except Exception as e:
            print(f"[인기도 랭킹] 페이지 {page} 오류: {e}")
            break

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.rank-table tr")

        page_has_friend = False
        for row in rows:
            # 순위: span.rank-total
            rank_el = row.select_one("span.rank-total")
            if not rank_el:
                continue
            rank_m = re.match(r"(\d+)", rank_el.get_text(strip=True))
            if not rank_m:
                continue
            server_pop_rank = int(rank_m.group(1))

            # 길드명: span.badge-guild
            guild_el = row.select_one("span.badge-guild")
            guild_name = guild_el.get_text(strip=True) if guild_el else ""
            if guild_name not in FRIEND_GUILDS:
                continue

            page_has_friend = True

            # 닉네임: span.nickname
            nick_el = row.select_one("span.nickname")
            if not nick_el:
                continue
            name = nick_el.get_text(strip=True)
            if not name:
                continue

            if name in remaining:
                found[name] = server_pop_rank
                remaining.discard(name)
                print(f"[인기도 랭킹] {name} → 서버 {server_pop_rank}위 발견")

        # 친구패밀리가 없는 페이지 3개 연속이면 조기 종료
        if page_has_friend:
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= 10 and page > 10:
                print(f"[인기도 랭킹] 빈 페이지 {empty_streak}연속 → 종료")
                break

        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"[인기도 랭킹] 수집 완료: {len(found)}명 / 전체 {len(member_names)}명")
    return found


def fetch_rival_guilds() -> tuple[list[dict], list[dict]]:
    """경쟁 길드 데이터 수집 → (길드 요약 리스트, 멤버 리스트)"""
    summaries = []
    all_members = []
    for guild_name, url in RIVAL_GUILDS.items():
        print(f"[경쟁 길드] 수집 중: {guild_name}")
        try:
            html = fetch_page(url, retries=2)
            # 디버그: HTML 첫 500자 + tr/power-tooltip 개수 확인
            from bs4 import BeautifulSoup as _BS
            _soup = _BS(html, "html.parser")
            _trs = _soup.select("tr")
            _pts = _soup.select(".power-tooltip")
            _char_links = _soup.select("a[href*='character.php']")
            print(f"  [디버그] tr={len(_trs)}, .power-tooltip={len(_pts)}, character links={len(_char_links)}")
            print(f"  [디버그] HTML 길이={len(html)}")
            summary, members = parse_rival_guild(html, guild_name)
            summaries.append(summary)
            all_members.extend(members)
            print(f"  -> 전투력: {summary['total_power']}, 인원: {summary['member_count']}명")
        except Exception as e:
            import traceback
            print(f"  -> 오류: {e}")
            print(traceback.format_exc())
        time.sleep(REQUEST_DELAY_SECONDS)
    return summaries, all_members