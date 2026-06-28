-- 스카니아11 서버 전체 길드 랭킹 (2026-06-28)
-- 홈 '스카니아11 길드 랭킹' 섹션 + 향후 길드 비교용.
-- run_server_guild_update()가 6시간마다 전량 교체(delete+insert)로 채움.
-- 적용: Supabase SQL Editor에서 실행.

create table if not exists server_guild_ranking (
  guild_rank       int         not null,          -- 서버 길드 순위
  guild_name       text        not null,
  level            int,
  members          int,
  power            bigint,                          -- 총전투력
  top_power        bigint,                          -- 길드 내 최고 멤버 전투력
  low_power        bigint,                          -- 길드 내 최저 멤버 전투력
  avg_member_power bigint,                          -- 멤버 평균 전투력(크롤 기준) — 균형 점수용
  captured_at      timestamptz not null default now(),
  primary key (guild_rank)
);

create index if not exists idx_sgr_name on server_guild_ranking(guild_name);

-- 이미 테이블이 있다면(균형 컬럼 추가):
-- alter table server_guild_ranking add column if not exists top_power bigint;
-- alter table server_guild_ranking add column if not exists low_power bigint;
-- alter table server_guild_ranking add column if not exists avg_member_power bigint;
