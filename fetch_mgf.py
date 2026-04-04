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
    page_text = soup.get_text("\n", strip=True)

    # "레벨" 키워드 다음 줄에서 Lv.XX 파싱
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if line == "레벨" and idx + 1 < len(lines):
            m = re.search(r"Lv\.?\s*(\d+)", lines[idx + 1])
            if m:
                return int(m.group(1))

    # 직접 패턴 매칭
    m = re.search(r"레벨\s*Lv\.?\s*(\d+)", page_text)
    if m:
        return int(m.group(1))
    m = re.search(r"Lv\.(\d+)", page_text)
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