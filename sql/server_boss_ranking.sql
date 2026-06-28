-- 스카니아11 서버 전체 보스 랭킹 (토벌전/월드보스) (2026-06-28)
-- 홈 '토벌전·월드보스 랭킹' 섹션용.
-- run_server_boss_update()가 6시간마다 kind별 전량 교체로 채움.
-- 적용: Supabase SQL Editor에서 실행.

create table if not exists server_boss_ranking (
  kind        text        not null,            -- 'guild_boss'(토벌전) | 'world_boss'(월드보스)
  server_rank int         not null,
  nickname    text,
  guild       text,
  score       bigint,
  score_text  text,
  level       int,
  job         text,
  captured_at timestamptz not null default now(),
  primary key (kind, server_rank)
);

create index if not exists idx_sbr_kind on server_boss_ranking(kind);
