-- 개인 업무 관리용 테이블 (single-user — 친구닷)
-- Supabase SQL Editor에서 한 번만 실행

-- 카테고리 (사용자가 직접 추가/삭제 가능)
create table if not exists personal_categories (
  id bigserial primary key,
  owner text not null,
  name text not null,
  color text default '#6366f1',
  sort_order int default 0,
  created_at timestamptz default now(),
  unique (owner, name)
);

create index if not exists personal_categories_owner_idx
  on personal_categories (owner, sort_order);

-- 업무
create table if not exists personal_tasks (
  id bigserial primary key,
  owner text not null,
  category text,                     -- 카테고리명을 그대로 저장 (FK 없음 — 카테고리 지워도 task는 남김)
  title text not null,
  notes text default '',
  status text not null default 'todo' check (status in ('todo','in_progress','waiting','done')),
  priority text not null default 'medium' check (priority in ('high','medium','low')),
  due_date date,
  tags text[] default '{}'::text[],
  sort_order int default 0,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  completed_at timestamptz
);

create index if not exists personal_tasks_owner_status_idx
  on personal_tasks (owner, status, sort_order);
create index if not exists personal_tasks_owner_due_idx
  on personal_tasks (owner, due_date);

-- 기본 카테고리 시드 (친구닷용)
insert into personal_categories (owner, name, color, sort_order) values
  ('친구닷', '쿠팡',      '#3b82f6', 1),
  ('친구닷', '결혼',      '#ec4899', 2),
  ('친구닷', '자기계발',  '#10b981', 3),
  ('친구닷', '사업준비',  '#f59e0b', 4),
  ('친구닷', '기타',      '#64748b', 5)
on conflict (owner, name) do nothing;
