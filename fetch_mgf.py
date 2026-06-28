import json
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup


def norm_name(s: str | None) -> str:
    """한글 NFC 정규화 + 공백 제거. 길드 페이지 닉네임과 랭킹 페이지 닉네임 매칭용."""
    return unicodedata.normalize("NFC", (s or "")).strip()

from config import (
    TARGET_GUILD_URLS,
    REQUEST_TIMEOUT,
    REQUEST_DELAY_SECONDS,
    DETAIL_REQUEST_DELAY_SECONDS,
    DETAIL_REQUEST_RETRIES,
    USER_AGENT,
    MAX_MEMBERS_PER_GUILD,
    EXCLUDED_MEMBER_NAMES,
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

# 거주지 프록시(선택): PROXY_URL 이 있으면 "서버 전체 풀크롤(fetch_server_top)"에만 적용한다.
# 멤버/보스/인기도/길드 크롤은 페이지가 적어 데이터센터 IP로도 충분 → 직접 연결 유지.
# (전부 프록시로 돌리면 동시 크롤들이 프록시를 한꺼번에 때려 혼잡 + 프록시 비용 급증)
# 미설정 시 전부 직접 연결(기존 동작). 형식: http://user:pass@host:port
_proxy_url = os.environ.get("PROXY_URL", "").strip()
SERVER_PROXY = {"http": _proxy_url, "https": _proxy_url} if _proxy_url else None
if SERVER_PROXY:
    print(f"[fetch_mgf] 서버 풀크롤 전용 프록시 적용 (호스트: {_proxy_url.split('@')[-1] if '@' in _proxy_url else _proxy_url})")

def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name)

def save_debug_html(guild_name: str, html: str) -> None:
    filename = Path(__file__).resolve().parent / f"debug_guild_{safe_filename(guild_name)}.html"
    filename.write_text(html, encoding="utf-8")

def fetch_page(url: str, retries: int = 1, proxies=None) -> str:
    last_error = None
    for _ in range(retries + 1):
        try:
            res = _session.get(url, timeout=REQUEST_TIMEOUT, proxies=proxies)
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
        # 서버순위·인기도는 더 이상 캐릭터 상세 페이지(JS 렌더링)에서 못 긁는다.
        # fetch_mgf_data()에서 랭킹 목록(index.php)을 일괄 조회해 아래 0 placeholder를 채운다.
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
            "overall_rank": 0,
            "server_rank": 0,
            "popularity": 0,
        })
    members = sorted(members, key=lambda x: x.get("guildRank", 9999))
    return members[:MAX_MEMBERS_PER_GUILD]

def fetch_mgf_data():
    print("=== MGF 길드 수집 시작 ===")
    all_members = []
    for guild_name, url in TARGET_GUILD_URLS.items():
        print(f"수집 중: {guild_name}")
        html = fetch_page(url, retries=1)
        save_debug_html(guild_name, html)
        guild_level = parse_guild_level(html)
        print(f" -> 길드 레벨: {guild_level}")
        members = parse_members_from_html(html, guild_name, guild_level)
        print(f" -> {len(members)}명")
        all_members.extend(members)
        time.sleep(REQUEST_DELAY_SECONDS)

    # 서버순위 + 인기도 일괄 수집 (상세 페이지가 JS 렌더링으로 바뀌어 목록 페이지에서 추출)
    names = {m["name"] for m in all_members}
    rank_map = fetch_server_ranking(names)
    matched = 0
    for m in all_members:
        info = rank_map.get(norm_name(m["name"]))
        if info:
            m["server_rank"] = info["server_rank"]
            m["popularity"] = info["popularity"]
            matched += 1
    print(f"서버순위/인기도 매칭: {matched}/{len(all_members)}명")

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


# ── 서버 전투력 랭킹(서버순위) + 인기도(♥) 값 크롤링 ──────────────
# 캐릭터 상세 페이지가 클라이언트(JS) 렌더링으로 바뀌어 requests로는 수치를 못 읽는다.
# 서버사이드 렌더링되는 랭킹 목록(index.php)에서 서버순위와 인기도를 한 번에 추출한다.
SERVER_RANK_URL = "https://mgf.gg/ranking/index.php?server=11&recent=1&stx=&page={page}"

def fetch_server_ranking(member_names: set[str], max_pages: int = 150) -> dict[str, dict]:
    """
    스카니아11 전투력 랭킹을 순회하며 친구패밀리 멤버의
    '서버 순위'와 '인기도(♥)' 값을 함께 수집.
    반환값: { 닉네임(NFC): {"server_rank": int, "popularity": int} }
    멤버 전원 발견 또는 친구길드 없는 페이지가 연속되면 조기 종료.
    """
    found: dict[str, dict] = {}
    remaining = {norm_name(n) for n in member_names}
    empty_streak = 0

    for page in range(1, max_pages + 1):
        if not remaining:
            break
        url = SERVER_RANK_URL.format(page=page)
        try:
            html = fetch_page(url, retries=2)
        except Exception as e:
            print(f"[서버 랭킹] 페이지 {page} 오류: {e}")
            break

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.rank-table tr")

        page_has_friend = False
        for row in rows:
            # index.php는 길드 뱃지가 <a class="badge-guild">, pop.php는 <span> — 태그 무관 클래스로 선택
            guild_el = row.select_one(".badge-guild")
            guild_name = norm_name(guild_el.get_text(strip=True)) if guild_el else ""
            if guild_name not in FRIEND_GUILDS:
                continue
            page_has_friend = True

            nick_el = row.select_one("span.nickname")
            if not nick_el:
                continue
            name = norm_name(nick_el.get_text(strip=True))
            if name not in remaining:
                continue

            rank_el = row.select_one("span.rank-total")
            server_rank = parse_number(rank_el.get_text(strip=True)) if rank_el else 0
            pop_el = row.select_one("span.badge-pop")  # "♥ 1,478"
            popularity = parse_number(pop_el.get_text(strip=True)) if pop_el else 0

            found[name] = {"server_rank": server_rank, "popularity": popularity}
            remaining.discard(name)
            print(f"[서버 랭킹] {name} → 서버 {server_rank}위 / ♥{popularity}")

        if page_has_friend:
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= 15 and page > 15:
                print(f"[서버 랭킹] 빈 페이지 {empty_streak}연속 → 종료")
                break

        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"[서버 랭킹] 수집 완료: {len(found)}명 / 전체 {len(member_names)}명")
    return found


# ── 서버 전체 랭킹 Top-N 수집 (전투력순, 길드 무관) ──────────────
# index.php는 30행/페이지. 3000명 = 100페이지. 멤버 필터 없이 상위 N명을 전부 수집한다.
def fetch_server_top(limit: int = 3000, max_pages: int = 110) -> list[dict]:
    """
    스카니아11 전투력 랭킹(index.php)을 순회하며 상위 limit명을 길드 무관 전부 수집.
    반환: [{"server_rank","nickname","guild","power","power_text","popularity","level","job"}...]
    """
    # Railway 데이터센터 IP는 mgf.gg에서 ~수십 페이지 후 rate-limit(429/차단) 걸리는 패턴.
    # 실패/빈페이지 시 백오프로 점점 길게 쉬며 "같은 페이지를 재시도"(순위 누락 방지).
    # 연속 실패가 한계를 넘으면 중단.
    DELAY = 1.0          # 페이지 간 기본 간격
    MAX_CONSEC = 18      # 캡차 IP 회피용: 실패할 때마다 출구 IP를 바꿔가며 재시도
    results: list[dict] = []
    consec_fail = 0
    page = 1

    # 서버 풀크롤 전용 세션(멤버 크롤의 _session과 분리). mgf가 IPRoyal 풀의 일부 출구 IP에
    # 캡차를 띄우므로, 캡차/실패를 만나면 연결을 닫아 새 출구 IP(IPRoyal randomize)로 회전한다.
    s = requests.Session()
    s.headers.update(HEADERS)
    if SERVER_PROXY:
        s.proxies.update(SERVER_PROXY)

    def _rotate_ip():
        if SERVER_PROXY:
            try:
                s.close()   # 연결 풀 종료 → 다음 요청은 새 연결 = 새 출구 IP
            except Exception:
                pass

    while page <= max_pages and len(results) < limit:
        url = SERVER_RANK_URL.format(page=page)
        try:
            res = s.get(url, timeout=REQUEST_TIMEOUT)
            res.encoding = "utf-8"
            html = res.text
        except Exception as e:
            consec_fail += 1
            _rotate_ip()
            backoff = min(2 * consec_fail, 12)
            print(f"[서버 전체] p{page} 오류:{repr(e)[:50]} (연속 {consec_fail}/{MAX_CONSEC}) → IP회전·{backoff}s")
            if consec_fail >= MAX_CONSEC:
                print("[서버 전체] 연속 실패 한계 → 중단")
                break
            time.sleep(backoff)
            continue   # 같은 page 재시도

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.rank-table tr")

        page_count = 0
        for row in rows:
            nick_el = row.select_one("span.nickname")
            rank_el = row.select_one("span.rank-total")
            if not nick_el or not rank_el:
                continue
            server_rank = parse_number(rank_el.get_text(strip=True))
            if not server_rank:
                continue

            guild_el = row.select_one(".badge-guild")
            guild = guild_el.get_text(strip=True) if guild_el else ""
            pow_el = row.select_one(".power-tooltip") or row.select_one(".power-kor")
            power_text = pow_el.get_text(strip=True) if pow_el else ""
            power = convert_korean_power_to_int(power_text) if power_text else 0
            pop_el = row.select_one("span.badge-pop")
            popularity = parse_number(pop_el.get_text(strip=True)) if pop_el else 0
            level_el = row.select_one("span.level")
            level = parse_number(level_el.get_text(strip=True)) if level_el else 0
            job_el = row.select_one("span.job-name")
            job = job_el.get_text(strip=True) if job_el else ""

            results.append({
                "server_rank": server_rank,
                "nickname": nick_el.get_text(strip=True),
                "guild": guild,
                "power": power,
                "power_text": power_text,
                "popularity": popularity,
                "level": level,
                "job": job,
            })
            page_count += 1
            if len(results) >= limit:
                break

        if page_count == 0:
            # 랭킹 테이블 구조는 있는데 데이터 행이 0 = 진짜 끝
            if "rank-table" in html:
                print(f"[서버 전체] p{page} 데이터 끝 → 종료 (수집 {len(results)})")
                break
            # 테이블 자체가 없음 = 캡차/차단 페이지 → 새 출구 IP로 같은 page 재시도
            consec_fail += 1
            is_cap = "captcha" in html.lower()
            _rotate_ip()
            backoff = min(2 * consec_fail, 12)
            print(f"[서버 전체] p{page} {'캡차' if is_cap else '빈페이지'} (연속 {consec_fail}/{MAX_CONSEC}) → IP회전·{backoff}s")
            if consec_fail >= MAX_CONSEC:
                print("[서버 전체] 캡차 한계 → 중단")
                break
            time.sleep(backoff)
            continue   # 같은 page 재시도(새 IP)
        consec_fail = 0
        page += 1      # 성공 시에만 다음 페이지로
        time.sleep(DELAY)

    results.sort(key=lambda x: x["server_rank"])
    print(f"[서버 전체] 수집 완료: {len(results)}명")
    return results


# ── 토벌전 / 월드보스 랭킹 (멤버별 점수 + 서버순위) ──────────────
# index.php와 동일한 행 구조 + score-cell(점수). 상세 페이지 대신 목록에서 추출.
BOSS_RANK_URLS = {
    "guild_boss": "https://mgf.gg/ranking/guild_boss.php?server=11&page={page}",   # 토벌전
    "world_boss": "https://mgf.gg/ranking/world_boss.php?server=11&page={page}",   # 월드보스
}

def fetch_boss_ranking(member_names: set[str], kind: str, max_pages: int = 150) -> dict[str, dict]:
    """
    kind: 'guild_boss'(토벌전) 또는 'world_boss'(월드보스).
    반환값: { 닉네임(NFC): {"rank": 서버순위, "score": 점수(int)} }
    """
    url_tpl = BOSS_RANK_URLS[kind]
    found: dict[str, dict] = {}
    remaining = {norm_name(n) for n in member_names}
    empty_streak = 0

    for page in range(1, max_pages + 1):
        if not remaining:
            break
        try:
            html = fetch_page(url_tpl.format(page=page), retries=2)
        except Exception as e:
            print(f"[{kind}] 페이지 {page} 오류: {e}")
            break

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.rank-table tr")
        page_has_friend = False
        for row in rows:
            guild_el = row.select_one(".badge-guild")
            guild_name = norm_name(guild_el.get_text(strip=True)) if guild_el else ""
            if guild_name not in FRIEND_GUILDS:
                continue
            page_has_friend = True

            nick_el = row.select_one("span.nickname")
            if not nick_el:
                continue
            name = norm_name(nick_el.get_text(strip=True))
            if name not in remaining:
                continue

            rank_el = row.select_one("span.rank-total")
            rank = parse_number(rank_el.get_text(strip=True)) if rank_el else 0
            score_el = row.select_one(".score-tooltip") or row.select_one(".score-kor")
            score = convert_korean_power_to_int(score_el.get_text(strip=True)) if score_el else 0

            found[name] = {"rank": rank, "score": score}
            remaining.discard(name)

        if page_has_friend:
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= 15 and page > 15:
                print(f"[{kind}] 빈 페이지 {empty_streak}연속 → 종료")
                break
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"[{kind}] 수집 완료: {len(found)}명 / 전체 {len(member_names)}명")
    return found


def fetch_boss_top(kind: str, limit: int = 100, max_pages: int = 60) -> list[dict]:
    """kind: 'guild_boss'(토벌전) / 'world_boss'(월드보스). 서버 전체 상위 limit명(길드 무관).
    반환: [{server_rank, nickname, guild, score, score_text, level, job}...]"""
    url_tpl = BOSS_RANK_URLS[kind]
    results: list[dict] = []
    seen: set = set()
    for page in range(1, max_pages + 1):
        if len(results) >= limit:
            break
        try:
            html = fetch_page(url_tpl.format(page=page), retries=2)
        except Exception as e:
            print(f"[{kind} top] 페이지 {page} 오류: {e}")
            break
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.rank-table tr")
        page_count = 0
        for row in rows:
            nick_el = row.select_one("span.nickname")
            rank_el = row.select_one("span.rank-total")
            if not nick_el or not rank_el:
                continue
            rank = parse_number(rank_el.get_text(strip=True))
            if not rank:
                continue
            nm = nick_el.get_text(strip=True)
            key = (rank, nm)
            if key in seen:
                continue
            guild_el = row.select_one(".badge-guild")
            guild = guild_el.get_text(strip=True) if guild_el else ""
            score_el = row.select_one(".score-tooltip") or row.select_one(".score-kor")
            score_text = score_el.get_text(strip=True) if score_el else ""
            score = convert_korean_power_to_int(score_text) if score_text else 0
            level_el = row.select_one("span.level")
            level = parse_number(level_el.get_text(strip=True)) if level_el else 0
            job_el = row.select_one("span.job-name")
            job = job_el.get_text(strip=True) if job_el else ""
            seen.add(key)
            results.append({
                "server_rank": rank, "nickname": nm, "guild": guild,
                "score": score, "score_text": score_text, "level": level, "job": job,
            })
            page_count += 1
            if len(results) >= limit:
                break
        if page_count == 0:
            break
        time.sleep(REQUEST_DELAY_SECONDS)
    results.sort(key=lambda x: x["server_rank"])
    print(f"[{kind} top] 수집 완료: {len(results)}명")
    return results


# ── 우리 친구 길드들의 서버 길드 랭킹 ───────────────────────────
GUILD_RANK_URL = "https://mgf.gg/ranking/guild_ranking.php?server=11&page={page}"
ACTUAL_FRIEND_GUILDS = {"친구들", "친구둘", "친구삼", "친구넷"}  # 실제 길드(친구닷=캐릭터 제외)

def fetch_guild_server_ranks(max_pages: int = 100) -> dict[str, dict]:
    """
    친구 길드들의 스카니아11 길드 서버순위/레벨/인원/총전투력.
    반환값: { 길드명(NFC): {"rank":int,"level":int,"members":int,"power":int} }
    """
    found: dict[str, dict] = {}
    remaining = set(ACTUAL_FRIEND_GUILDS)
    empty_streak = 0

    for page in range(1, max_pages + 1):
        if not remaining:
            break
        try:
            html = fetch_page(GUILD_RANK_URL.format(page=page), retries=2)
        except Exception as e:
            print(f"[길드 랭킹] 페이지 {page} 오류: {e}")
            break

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.rank-table tr")
        page_has_friend = False
        for row in rows:
            gname_el = row.select_one(".guild-name")
            if not gname_el:
                continue
            gname = norm_name(gname_el.get_text(strip=True))
            if gname not in remaining:
                continue
            page_has_friend = True

            rank_el = row.select_one(".rank-cell")
            rank = parse_number(rank_el.get_text(strip=True)) if rank_el else 0
            level_el = row.select_one(".guild-level")
            level = parse_number(level_el.get_text(strip=True)) if level_el else 0
            power_el = row.select_one(".power-tooltip")
            power = convert_korean_power_to_int(power_el.get_text(strip=True)) if power_el else 0
            mm = re.search(r"(\d+)\s*명", row.get_text(" ", strip=True))
            members = int(mm.group(1)) if mm else 0

            found[gname] = {"rank": rank, "level": level, "members": members, "power": power}
            remaining.discard(gname)
            print(f"[길드 랭킹] {gname} → 서버 {rank}위 / Lv.{level} / {members}명 / {power}")

        if page_has_friend:
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= 10 and page > 5:
                print(f"[길드 랭킹] 빈 페이지 {empty_streak}연속 → 종료")
                break
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"[길드 랭킹] 수집 완료: {len(found)}/{len(ACTUAL_FRIEND_GUILDS)}개 길드")
    return found


def fetch_server_guild_top(limit: int = 30, max_pages: int = 12) -> list[dict]:
    """스카니아11 길드 서버순위 상위 limit개 (길드 무관 전부).
    fetch_guild_server_ranks와 같은 페이지(.guild-name/.rank-cell/.guild-level/.power-tooltip) 파싱.
    반환: [{"guild_rank","guild_name","level","members","power"}...]"""
    results: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        if len(results) >= limit:
            break
        try:
            html = fetch_page(GUILD_RANK_URL.format(page=page), retries=2)
        except Exception as e:
            print(f"[서버 길드] 페이지 {page} 오류: {e}")
            break
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table.rank-table tr")
        page_count = 0
        for row in rows:
            gname_el = row.select_one(".guild-name")
            if not gname_el:
                continue
            gname = norm_name(gname_el.get_text(strip=True))
            if not gname or gname in seen:
                continue
            rank_el = row.select_one(".rank-cell")
            rank = parse_number(rank_el.get_text(strip=True)) if rank_el else 0
            if not rank:
                continue
            level_el = row.select_one(".guild-level")
            level = parse_number(level_el.get_text(strip=True)) if level_el else 0
            power_el = row.select_one(".power-tooltip")
            power = convert_korean_power_to_int(power_el.get_text(strip=True)) if power_el else 0
            mm = re.search(r"(\d+)\s*명", row.get_text(" ", strip=True))
            members = int(mm.group(1)) if mm else 0
            seen.add(gname)
            results.append({"guild_rank": rank, "guild_name": gname,
                            "level": level, "members": members, "power": power})
            page_count += 1
            if len(results) >= limit:
                break
        if page_count == 0:
            break
        time.sleep(REQUEST_DELAY_SECONDS)
    results.sort(key=lambda x: x["guild_rank"])
    print(f"[서버 길드] 수집 완료: {len(results)}개")
    return results


def fetch_guild_member_powers(guild_name: str) -> list[int]:
    """길드 페이지에서 멤버 전투력만 경량 추출(상세페이지 호출 X). 길드 전력 균형 점수용."""
    url = "https://mgf.gg/contents/guild_info.php?g_name=" + requests.utils.quote(guild_name)
    try:
        html = fetch_page(url, retries=1)
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    powers: list[int] = []
    for row in soup.select(".members-list .member-row"):
        pe = row.select_one(".power-tooltip") or row.select_one(".power-text")
        if not pe:
            continue
        p = convert_korean_power_to_int(pe.get_text(" ", strip=True))
        if p > 0:
            powers.append(p)
    return powers


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