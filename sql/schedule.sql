-- 콘텐츠 일정 (길드라운지 앱) — 2026-06-05
-- 설계: guild-app-54/docs/일정_백엔드화_설계.md
-- 적용: Supabase SQL Editor에서 실행. 모든 시각은 KST(+09:00) 명시.

-- ── 콘텐츠 메타 ───────────────────────────────────────────────
create table if not exists contents (
  id          text primary key,           -- tobeol/daehang/boss/colo/suryeon
  name        text not null,
  icon        text not null,
  type        text not null check (type in ('always','season')),
  recurrence  jsonb,                       -- always형만: {"weekday":1,"hour":0,"min":0,"durationMin":10079} (weekday 0=일..6=토, KST)
  sort_order  int  not null default 0,
  active      bool not null default true
);

-- ── 시즌 회차 (운영진 입력분만 저장. 상시는 서버가 규칙으로 펼침) ──
create table if not exists occurrences (
  id          bigint generated always as identity primary key,
  content_id  text not null references contents(id) on delete cascade,
  round_label text,                        -- '1회차'
  start_at    timestamptz not null,
  end_at      timestamptz not null,
  created_at  timestamptz not null default now()
);
create index if not exists idx_occurrences_content on occurrences(content_id);
create index if not exists idx_occurrences_start   on occurrences(start_at);

-- ── 콘텐츠 메타 시드 ──────────────────────────────────────────
-- 상시 duration(분): 토벌전 월00:00→일23:59 = 6일23시간59분 = 10079
--                    대항전 목12:00→월22:00 = 4일10시간       = 6360
insert into contents (id, name, icon, type, recurrence, sort_order) values
  ('tobeol',  '길드 토벌전',    '⚔️', 'always', '{"weekday":1,"hour":0,"min":0,"durationMin":10079}', 1),
  ('daehang', '길드 대항전',    '🛡️', 'always', '{"weekday":4,"hour":12,"min":0,"durationMin":6360}', 2),
  ('boss',    '길드 보스 대전', '🔥', 'season', null, 3),
  ('colo',    '콜로세움',       '🏟️', 'season', null, 4),
  ('suryeon', '길드 수련장',    '🏋️', 'season', null, 5)
on conflict (id) do update
  set name = excluded.name, icon = excluded.icon, type = excluded.type,
      recurrence = excluded.recurrence, sort_order = excluded.sort_order;

-- ── 현재 시즌 회차 시드 (2026-05~06 시즌, KST) ────────────────
-- 보스 대전: 금 12:00 → 수 22:00, 3회차
insert into occurrences (content_id, round_label, start_at, end_at) values
  ('boss', '1회차', '2026-05-22T12:00:00+09:00', '2026-05-27T22:00:00+09:00'),
  ('boss', '2회차', '2026-05-29T12:00:00+09:00', '2026-06-03T22:00:00+09:00'),
  ('boss', '3회차', '2026-06-05T12:00:00+09:00', '2026-06-10T22:00:00+09:00'),
-- 콜로세움: 화 12:00 → 토 22:00, 3회차
  ('colo', '1회차', '2026-05-26T12:00:00+09:00', '2026-05-30T22:00:00+09:00'),
  ('colo', '2회차', '2026-06-02T12:00:00+09:00', '2026-06-06T22:00:00+09:00'),
  ('colo', '3회차', '2026-06-09T12:00:00+09:00', '2026-06-13T22:00:00+09:00');
-- 수련장: 현재 진행 시즌 없음 → 시드 없음. 새 시즌 시 운영진이 등록.
