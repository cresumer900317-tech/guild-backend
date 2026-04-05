from fetch_mgf import load_snapshot
from config import PREVIOUS_SNAPSHOT_PATH

def safe_rate(curr: int, prev: int) -> float:
    if prev <= 0:
        return 0.0
    return ((curr - prev) / prev) * 100.0

def trend_direction(diff: int) -> str:
    if diff > 0:
        return "up"
    if diff < 0:
        return "down"
    return "same"

def transform_data(raw_data):
    prev_snapshot = load_snapshot(PREVIOUS_SNAPSHOT_PATH)
    prev_rows = prev_snapshot.get("rows", []) if isinstance(prev_snapshot, dict) else []
    prev_map = {row.get("name"): row for row in prev_rows if row.get("name")}

    enriched = []
    for item in raw_data:
        prev = prev_map.get(item.get("name"), {})
        power_prev = int(prev.get("power", 0) or 0)
        server_prev = int(prev.get("server_rank", 0) or 0)
        current_power = int(item.get("power", 0) or 0)
        current_server = int(item.get("server_rank", 0) or 0)
        growth_abs = current_power - power_prev if power_prev else 0
        growth_rate = safe_rate(current_power, power_prev) if power_prev else 0.0
        server_rank_diff = server_prev - current_server if current_server and server_prev else 0
        enriched.append({
            **item,
            "power_prev": power_prev,
            "growth_abs": growth_abs,
            "growth_rate": round(growth_rate, 2),
            "server_rank_prev": server_prev,
            "server_rank_diff": server_rank_diff,
            "trend_direction": trend_direction(server_rank_diff),
            "weeklyDiff": growth_abs,
        })

    ranking_sorted = sorted(enriched, key=lambda x: x.get("power", 0), reverse=True)
    ranking_data = []
    for idx, item in enumerate(ranking_sorted, start=1):
        ranking_data.append({
            "rank": idx,
            "guildRank": item.get("guildRank", 0),
            "name": item.get("name", "알수없음"),
            "job": item.get("job", "미확인"),
            "power": item.get("power", 0),
            "powerText": item.get("power_text", "0"),
            "powerPrev": item.get("power_prev", 0),
            "guild": item.get("guild", "길드 없음"),
            "image": item.get("image", ""),
            "weeklyDiff": item.get("growth_abs", 0),
            "growthRate": item.get("growth_rate", 0.0),
            "guildLevel": item.get("guild_level", 0),
            "level": item.get("level", 0),
            "overallRank": item.get("overall_rank", 0),
            "serverRank": item.get("server_rank", 0),
            "serverRankPrev": item.get("server_rank_prev", 0),
            "serverRankDiff": item.get("server_rank_diff", 0),
            "serverRankDirection": item.get("trend_direction", "same"),
            "popularity": item.get("popularity", 0),
            "detailUrl": item.get("detail_url", ""),
            "isMaster": item.get("is_master", False),
            "capturedAt": item.get("capturedAt"),
        })

    weekly_sorted = sorted(ranking_data, key=lambda x: (x.get("weeklyDiff", 0), x.get("growthRate", 0.0), x.get("serverRankDiff", 0)), reverse=True)
    active_server_ranks = [x["serverRank"] for x in ranking_data if x.get("serverRank", 0) > 0]
    avg_power = int(sum(x["power"] for x in ranking_data) / len(ranking_data)) if ranking_data else 0
    avg_server_rank = round(sum(active_server_ranks) / len(active_server_ranks), 1) if active_server_ranks else 0

    home_summary = {
        "guild_name": "친구패밀리",
        "guild_count": len({x["guild"] for x in ranking_data}),
        "member_count": len(ranking_data),
        "avg_power": avg_power,
        "avg_server_rank": avg_server_rank,
    }

    return {"home_summary": home_summary, "members": ranking_data, "ranking": ranking_data, "weekly": weekly_sorted}