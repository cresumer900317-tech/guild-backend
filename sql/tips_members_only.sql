-- 공략 게시판 글 단위 "길드원 전용" 옵션 (2026-07-29)
-- Supabase SQL Editor 에서 한 번 실행 (멱등)
alter table tips add column if not exists members_only boolean not null default false;
