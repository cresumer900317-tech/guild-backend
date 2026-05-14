-- AI 토큰·비용 사용량 기록 (Phase 8)
-- Supabase SQL Editor 에서 한 번만 실행

create table if not exists personal_ai_usage (
  id bigserial primary key,
  owner text not null,
  kind text not null,                -- 'briefing' | 'inbox_classify' | 'daily_log_extract' | 'search' | 'other'
  model text not null default '',
  input_tokens int not null default 0,
  output_tokens int not null default 0,
  cost_usd numeric(10,6) not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists personal_ai_usage_owner_created_idx
  on personal_ai_usage (owner, created_at desc);

create index if not exists personal_ai_usage_owner_kind_idx
  on personal_ai_usage (owner, kind);
