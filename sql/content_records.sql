-- 컨텐츠 기록 (길드 회차별 점수) — 2026-08-02
-- 홈 '컨텐츠 기록' 섹션. 운영진이 어드민에서 회차별 점수 입력.
-- contents(길드 대항전=daehang, 토벌전=tobeol 등) 참조. 적용: Supabase SQL Editor에서 실행.

create table if not exists content_records (
  id            bigint generated always as identity primary key,
  content_id    text not null references contents(id) on delete cascade,
  round_label   text,                     -- '34회차' / '8/1'
  score         bigint not null default 0,
  participants  int,                       -- 참여 인원
  goal          bigint,                    -- 시즌 목표점수(진행바용, 선택)
  recorded_date date not null default (now() at time zone 'Asia/Seoul')::date,
  note          text,
  created_at    timestamptz not null default now()
);
create index if not exists idx_content_records on content_records(content_id, recorded_date desc, id desc);
