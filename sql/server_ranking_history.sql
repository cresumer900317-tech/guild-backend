-- 서버 랭킹 일별 이력 — 개인 프로필 성장 그래프용 (2026-06-27)
-- server_ranking 테이블은 "최신값만"(매 크롤 전량교체)이라 시계열이 없다.
-- 이 테이블에 하루 1행씩 누적해 두면 외부인 포함 누구나 성장 그래프를 그릴 수 있다.
-- 적용: Supabase SQL Editor에서 실행.

create table if not exists server_ranking_history (
  snapshot_date date        not null,           -- 스냅샷 날짜 (하루 1행)
  name          text        not null,
  server_rank   int,
  guild         text,
  power         bigint,
  popularity    int,
  captured_at   timestamptz not null default now(),
  primary key (snapshot_date, name)              -- 같은 날 재실행 시 덮어씀(idempotent)
);

create index if not exists idx_srh_name on server_ranking_history(name);
create index if not exists idx_srh_date on server_ranking_history(snapshot_date);
