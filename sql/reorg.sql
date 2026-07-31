-- 2026-08 운영개편 배치 보드 (운영진 전용) — 2026-07-31
-- 카톡 옵트인 명단을 5개 길드에 배치하고 인게임 이동(8/1~2)을 추적.
-- 적용: Supabase SQL Editor에서 실행.

create table if not exists reorg_board (
  id             bigint generated always as identity primary key,
  character_name text not null unique,            -- NFC 정규화해 저장
  assigned_guild text,                            -- 친구들|친구둘|친구삼|친구넷|친구닷, null=미배치
  moved          boolean not null default false,  -- 인게임 이동 완료 여부
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

-- 백엔드(service key)만 접근 — RLS 켜고 정책 없음
alter table reorg_board enable row level security;
