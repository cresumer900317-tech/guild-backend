"""Bounded public MGF guild observations, with source date separate from fetch time.
No database writes, no arbitrary URLs, no retries through access challenges.
"""
from datetime import datetime, timezone, date
from threading import Lock
from urllib.request import Request, urlopen
import re
import time
from bs4 import BeautifulSoup
from config import TARGET_GUILD_URLS

_cache = {}
_locks = {name: Lock() for name in TARGET_GUILD_URLS}
_attempted = {}


def korean_number(text):
    text = str(text or '').replace(',', '').strip()
    if re.fullmatch(r'\d+', text):
        return int(text)
    units = {'해':10**20,'경':10**16,'조':10**12,'억':10**8,'만':10**4}
    parts = re.findall(r'(\d+)\s*([해경조억만])', text)
    return sum(int(n)*units[u] for n,u in parts) if parts else None


def parse_guild_source(html, guild):
    soup=BeautifulSoup(html,'html.parser')
    title=soup.select_one('.guild-name')
    if not title or title.get_text(strip=True)!=guild:
        raise ValueError('MGF guild page missing or changed')
    stamp=soup.select_one('.guild-update-row')
    match=re.search(r'(\d{4})[.\-/](\d{2})[.\-/](\d{2})',stamp.get_text() if stamp else '')
    source_date=date(*map(int,match.groups())).isoformat() if match else None
    rows=[]
    for row in soup.select('.members-list .member-row'):
        name=row.select_one('.nick-link');sub=row.select_one('.member-sub')
        if not name or not sub:continue
        text=sub.get_text(' ',strip=True)
        level=re.search(r'Lv\.?\s*(\d+)',text)
        power=row.select_one('.only-bp .power-tooltip') or row.select_one('.only-bp .power-text')
        boss=row.select_one('.only-gb .power-tooltip')
        rows.append({'name':name.get_text(strip=True), 'job':re.sub(r'\s*\|?\s*Lv\.?\s*\d+','',text).strip(),
                     'level':int(level[1]) if level else None,
                     'power':int(row['data-bp']) if str(row.get('data-bp','')).isdigit() else korean_number(power.get_text() if power else ''),
                     'bossScore':int(row['data-gb']) if str(row.get('data-gb','')).isdigit() else korean_number(boss.get_text() if boss else '')})
    if not rows:raise ValueError('MGF member rows missing')
    bp=soup.select_one('.guild-stats .metric-bp .power-tooltip')
    gb=soup.select_one('.guild-stats .metric-gb .power-tooltip')
    return {'guild':guild,'sourceDate':source_date,'fetchedAt':datetime.now(timezone.utc).isoformat(),
            'sourceUrl':TARGET_GUILD_URLS[guild],'memberCount':len(rows),
            'totalPower':korean_number(bp.get_text() if bp else ''),
            'bossScore':korean_number(gb.get_text() if gb else ''),'members':rows,'stale':False}


def remember_guild_html(guild,html):
    result=parse_guild_source(html,guild)
    _cache[guild]=(time.monotonic(),result)
    return result


def get_guild_source(guild):
    if guild not in TARGET_GUILD_URLS:raise ValueError('Unknown guild')
    with _locks[guild]:
        cached=_cache.get(guild);now=time.monotonic()
        if cached and now-cached[0]<3600:return dict(cached[1])
        if now-_attempted.get(guild,-3600)<300:
            if cached:return {**cached[1],'stale':True}
            raise RuntimeError('MGF temporarily unavailable')
        _attempted[guild]=now
        try:
            request=Request(TARGET_GUILD_URLS[guild],headers={'User-Agent':'FriendsGuildLounge/1.0 (+https://xn--2e0br5l24w.com)','Accept-Language':'ko-KR'})
            with urlopen(request,timeout=10) as response:html=response.read(2_000_000).decode('utf-8')
            return remember_guild_html(guild,html)
        except Exception:
            if cached:return {**cached[1],'stale':True}
            raise RuntimeError('MGF temporarily unavailable') from None
