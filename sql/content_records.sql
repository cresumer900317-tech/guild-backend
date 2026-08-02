-- 컨텐츠 기록 (길드 회차별 점수) — 2026-08-02
-- 홈 '컨텐츠 기록' 섹션. 고정(대항전·토벌전) + 현재 진행 시즌 1개 + 지난 시즌 아카이브.
-- 적용: Supabase SQL Editor에서 실행.

-- 회차별 점수 기록
create table if not exists content_records (
  id            bigint generated always as identity primary key,
  content_id    text not null references contents(id) on delete cascade,
  round_label   text,                     -- '34회차' / '3주차'
  score         bigint not null default 0,
  participants  int,                       -- 참여 인원
  goal          bigint,                    -- (구) 목표점수 — 미사용, 진행바는 주차 기반
  recorded_date date not null default (now() at time zone 'Asia/Seoul')::date,
  note          text,
  created_at    timestamptz not null default now()
);
create index if not exists idx_content_records on content_records(content_id, recorded_date desc, id desc);

-- contents(메타)에 시즌 진행 정보 추가 — 시즌은 한 번에 하나만 is_current=true
alter table contents add column if not exists is_current boolean not null default false;
alter table contents add column if not exists starts_at  date;   -- 시즌 시작일(주차·진행바 계산)
alter table contents add column if not exists ends_at    date;   -- 시즌 종료일
