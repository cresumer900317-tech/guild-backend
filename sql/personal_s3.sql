-- 개인 업무 관리 — S3 확장 (Plan vs Actual 간트)
-- Supabase SQL Editor 에서 한 번만 실행

-- task 의 '실제' 작업 기간 — 계획(start_date~due_date)과 비교용
alter table personal_tasks
  add column if not exists actual_start_date date;
alter table personal_tasks
  add column if not exists actual_end_date date;
