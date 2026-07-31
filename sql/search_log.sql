-- 인기 검색어용 전적검색 로그 — 2026-07-31
-- profile 검색 성공 시 기록 → 홈 히어로 "인기 검색어" 칩 (최근 7일 집계)
-- 적용: Supabase SQL Editor에서 실행.

create table if not exists search_log (
  id          bigint generated always as identity primary key,
  name        text not null,              -- 검색된 캐릭터명 (NFC)
  searched_at timestamptz not null default now()
);
create index if not exists search_log_time_idx on search_log (searched_at desc);

alter table search_log enable row level security;
