-- 개인 업무 관리 확장 (Inbox / Projects / Daily Logs)
-- Supabase SQL Editor에서 한 번만 실행

-- ── 1) 프로젝트 ────────────────────────────────────────────
create table if not exists personal_projects (
  id bigserial primary key,
  owner text not null,
  name text not null,
  description text default '',
  status text not null default 'active' check (status in ('active','paused','done','dropped')),
  start_date date,
  end_date date,
  progress_pct int default 0 check (progress_pct between 0 and 100),
  color text default '#6366f1',
  notes text default '',
  sort_order int default 0,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists personal_projects_owner_idx
  on personal_projects (owner, sort_order, id);
create index if not exists personal_projects_owner_status_idx
  on personal_projects (owner, status);

-- ── 2) Tasks 에 project_id 추가 ────────────────────────────
alter table personal_tasks
  add column if not exists project_id bigint references personal_projects(id) on delete set null;

create index if not exists personal_tasks_owner_project_idx
  on personal_tasks (owner, project_id);

-- ── 3) Inbox (즉흥 입력 — 미처리 상자) ─────────────────────
create table if not exists personal_inbox (
  id bigserial primary key,
  owner text not null,
  content text not null,
  processed boolean not null default false,
  promoted_task_id bigint references personal_tasks(id) on delete set null,
  created_at timestamptz default now(),
  processed_at timestamptz
);

create index if not exists personal_inbox_owner_processed_idx
  on personal_inbox (owner, processed, created_at desc);

-- ── 4) Daily Logs (그날 뭐 했는지) ─────────────────────────
create table if not exists personal_daily_logs (
  id bigserial primary key,
  owner text not null,
  log_date date not null default current_date,
  content text not null default '',
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (owner, log_date)
);

create index if not exists personal_daily_logs_owner_date_idx
  on personal_daily_logs (owner, log_date desc);

-- ── 5) AI 정리 결과 캐시 (선택) ────────────────────────────
-- 같은 입력으로 또 호출하지 않게 결과 보관
create table if not exists personal_ai_summaries (
  id bigserial primary key,
  owner text not null,
  kind text not null,                 -- 'inbox_classify' | 'daily_digest' | 'briefing' 등
  source_hash text,                   -- 입력 해시 (중복 호출 방지)
  payload jsonb not null,             -- 결과 JSON
  created_at timestamptz default now()
);

create index if not exists personal_ai_summaries_owner_kind_idx
  on personal_ai_summaries (owner, kind, created_at desc);
