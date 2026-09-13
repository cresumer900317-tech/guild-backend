"""Pure, snapshot-aware calculations shared by the lounge endpoints.

Legacy captured_at values were written by Railway in UTC without an offset.
Missing observations are deliberately not treated as zero power/growth.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import unicodedata

KST = ZoneInfo("Asia/Seoul")


def name_key(value):
    return unicodedata.normalize("NFC", str(value or "")).strip()


def captured_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def latest_members(rows):
    best = {}
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for row in rows:
        key = name_key(row.get("name"))
        if not key:
            continue
        old = best.get(key)
        if old is None or (captured_time(row.get("captured_at")) or minimum) > (captured_time(old.get("captured_at")) or minimum):
            best[key] = row
    return list(best.values())


def current_server_rows(server_rows, members):
    """Latest known character stats, with the server rank's own timestamp intact.

Never rerank a partial roster against an older full-server snapshot, or invent
a server rank for a character absent from that snapshot.
"""
    member_map = {name_key(m.get("name")): m for m in latest_members(members)}
    out = []
    for original in server_rows:
        row = dict(original)
        row["rank_captured_at"] = original.get("captured_at")
        row["stats_source"] = "server"
        member = member_map.get(name_key(row.get("nickname")))
        newer = captured_time(member.get("captured_at")) if member else None
        previous = captured_time(row.get("captured_at"))
        if newer and (previous is None or newer > previous) and int(member.get("power") or 0) > 0:
            for field in ("power", "power_text", "guild", "job", "level"):
                if member.get(field) is not None:
                    row[field] = member[field]
            # Popularity has an independent collector; do not overwrite it with
            # a parser placeholder from a failed rank-page match.
            row["captured_at"] = member["captured_at"]
            row["stats_source"] = "guild"
        out.append(row)
    return out


def roster_history(history, members, today):
    roster = {name_key(m.get("name")): m for m in latest_members(members) if int(m.get("power") or 0) > 0}
    days = {}
    for row in history:
        key, day = name_key(row.get("name")), str(row.get("snapshot_date") or "")[:10]
        if key in roster and day and day <= today.isoformat() and int(row.get("power") or 0) > 0:
            days.setdefault(day, {})[key] = int(row["power"])
    # Add current observed values only on their actual observation date.
    for key, row in roster.items():
        captured = captured_time(row.get("captured_at"))
        if captured:
            day = captured.astimezone(KST).date().isoformat()
            if day <= today.isoformat():
                days.setdefault(day, {})[key] = int(row["power"])
    return roster, dict(sorted(days.items()))


def growth_story(history, members, today):
    roster, days = roster_history(history, members, today)
    dates = list(days)
    last = dates[-1] if dates else None
    prev = dates[-2] if len(dates) > 1 else None
    feed = []
    if last and prev:
        for key in days[last].keys() & days[prev].keys():
            before, after = days[prev][key], days[last][key]
            if after > before:
                feed.append({"name": key, "guild": roster[key].get("guild"), "diff": after-before,
                             "pct": round((after / before - 1) * 100, 2)})
    feed.sort(key=lambda r: (-r["diff"], r["name"]))
    # Only complete snapshots of the SAME current roster are comparable totals.
    totals = [(d, sum(values.values())) for d, values in days.items() if roster and len(values) == len(roster)]
    peak_day, peak = max(totals, key=lambda t: t[1]) if totals else (None, None)
    current = sum(int(m["power"]) for m in roster.values())
    unit = 10 * 10**16
    target = (current // unit + 1) * unit if current else None
    streak = 0
    complete_past = [(d, t) for d, t in totals if d < today.isoformat()]
    if not complete_past or complete_past[-1][0] != (today-timedelta(days=1)).isoformat():
        complete_past = []
    for i in range(len(complete_past)-1, 0, -1):
        d, value = complete_past[i]
        before_day, before_value = complete_past[i-1]
        if (date.fromisoformat(d)-date.fromisoformat(before_day)).days != 1 or value <= before_value:
            break
        streak += 1
    milestones, seen = [], 0
    for d, total in totals:
        level = total // unit * 10
        if level > seen:
            milestones.append({"gyeong": level, "date": d})
            seen = level
    return {
        "feed": feed[:12], "grewCount": len(feed), "feedDate": last, "prevDate": prev,
        "comparisonDays": (date.fromisoformat(last)-date.fromisoformat(prev)).days if last and prev else None,
        "peak": {"total": peak, "date": peak_day, "isToday": peak_day == today.isoformat()},
        "streakDays": streak, "milestones": milestones[-3:], "days": len(totals),
        "goal": {"target": target, "current": current, "remaining": target-current if target else None},
        "scope": "현재 친구패밀리 구성원", "historyWindowDays": 60,
        "sampledMembers": len(days[last]) if last else 0, "totalMembers": len(roster),
        "capturedAt": max((m.get("captured_at") or "" for m in roster.values()), default=None),
    }


def guild_dashboard(history, members, today):
    roster, days = roster_history(history, members, today)
    month_days = [d for d in days if d >= today.replace(day=1).isoformat()]
    # Fixed cohort across ALL displayed days prevents dips from missing rows.
    common = set.intersection(*(set(days[d]) for d in month_days)) if month_days else set()
    series = [{"date": d, "total": sum(days[d][n] for n in common)} for d in month_days] if common else []
    first, last = (series[0]["total"], series[-1]["total"]) if len(series) > 1 else (0, 0)
    dates = list(days)
    recent = len(dates) > 1 and dates[-1] == today.isoformat() and dates[-2] == (today-timedelta(days=1)).isoformat()
    growers = sum(days[dates[-1]][n] > days[dates[-2]][n] for n in days[dates[-1]].keys() & days[dates[-2]].keys()) if recent else None
    return {"series": series, "growthPct": round((last/first-1)*100, 2) if first else None,
            "growersYesterday": growers, "totalMembers": len(roster), "comparedMembers": len(common),
            "growersMonth": sum(days[month_days[-1]][n] > days[month_days[0]][n] for n in common) if len(month_days)>1 else None,
            "monthLabel": f"{today.month}월", "days": len(series),
            "scope": "모든 비교일에 기록이 있는 동일 구성원", "asOf": dates[-1] if dates else None}
