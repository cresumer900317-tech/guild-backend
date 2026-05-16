-- 개인 업무 관리 — S2 확장 (task 시작일 + 하위 작업)
-- Supabase SQL Editor 에서 한 번만 실행

-- task 에 시작일 (간트/타임라인 뷰의 막대 시작점)
alter table personal_tasks
  add column if not exists start_date date;

-- 하위 작업 (체크리스트) — 상위 task 삭제 시 함께 삭제
alter table personal_tasks
  add column if not exists parent_task_id bigint
  references personal_tasks(id) on delete cascade;

create index if not exists personal_tasks_owner_parent_idx
  on personal_tasks (owner, parent_task_id);
