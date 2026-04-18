from pathlib import Path

RAW_DIR = Path(__file__).resolve().parent / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

LATEST_SNAPSHOT_PATH = RAW_DIR / "latest_snapshot.json"
PREVIOUS_SNAPSHOT_PATH = RAW_DIR / "previous_snapshot.json"

REQUEST_TIMEOUT = 20
REQUEST_DELAY_SECONDS = 0.8
DETAIL_REQUEST_DELAY_SECONDS = 0.35
DETAIL_REQUEST_RETRIES = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0 Safari/537.36"
)

TARGET_GUILD_URLS = {
    "친구들": "https://mgf.gg/contents/guild_info.php?g_name=%EC%B9%9C%EA%B5%AC%EB%93%A4",
    "친구둘": "https://mgf.gg/contents/guild_info.php?g_name=%EC%B9%9C%EA%B5%AC%EB%91%98",
    "친구삼": "https://mgf.gg/contents/guild_info.php?g_name=%EC%B9%9C%EA%B5%AC%EC%82%BC",
    "친구넷": "https://mgf.gg/contents/guild_info.php?g_name=%EC%B9%9C%EA%B5%AC%EB%84%B7",
    "친구닷": "https://mgf.gg/contents/guild_info.php?g_name=%EC%B9%9C%EA%B5%AC%EB%8B%B7",
}

MAX_MEMBERS_PER_GUILD = 30

# 길드 목록에서 누락되더라도 반드시 수집할 멤버 (개별 캐릭터 페이지에서 직접 수집)
FORCE_INCLUDE_MEMBERS = {
    "임차돌",
    "갓친",
    "마캐",
    "뼝규",
    "악용자",
}

EXCLUDED_MEMBER_NAMES = {
    "9966",
    "칭구들",
    "개군보",
    "애정해",
    "유지율",
    "시크릿성쥬쥬",
    "겨울호떡",
    "마라탕수육",
}