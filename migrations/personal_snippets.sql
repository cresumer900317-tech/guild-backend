-- 코드 전달함 (Snippets) — 맥↔회사 노트북 코드 브릿지
-- Supabase SQL Editor 에서 1회 실행. 백엔드는 SERVICE_KEY(service_role)로
-- 접근하므로 RLS 정책 불필요 (소유권은 코드의 owner=character_name 으로 스코프).

create table if not exists personal_snippets (
  id          bigint generated always as identity primary key,
  owner       text        not null,
  title       text        not null default '',
  kind        text        not null default 'single',   -- 'single' | 'tb4'
  content     text        not null default '',          -- kind=single 본문
  html        text        not null default '',          -- kind=tb4 (ThingsBoard 4파트)
  css         text        not null default '',
  js          text        not null default '',
  settings    text        not null default '',
  sort_order  int         not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists personal_snippets_owner_idx on personal_snippets (owner);
