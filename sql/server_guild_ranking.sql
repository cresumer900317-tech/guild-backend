-- 스카니아11 서버 전체 길드 랭킹 (2026-06-28)
-- 홈 '스카니아11 길드 랭킹' 섹션 + 향후 길드 비교용.
-- run_server_guild_update()가 6시간마다 전량 교체(delete+insert)로 채움.
-- 적용: Supabase SQL Editor에서 실행.

create table if not exists server_guild_ranking (
  guild_rank  int         not null,          -- 서버 길드 순위
  guild_name  text        not null,
  level       int,
  members     int,
  power       bigint,
  captured_at timestamptz not null default now(),
  primary key (guild_rank)
);

create index if not exists idx_sgr_name on server_guild_ranking(guild_name);
