-- UGC 신고/차단 (App Store 가이드라인 1.2) — 2026-06-18
-- 적용: Supabase SQL Editor에서 실행.

-- 신고 접수 (운영진이 검토 → status 처리)
create table if not exists reports (
  id          bigint generated always as identity primary key,
  reporter    text not null,                -- 신고한 사람
  target_type text not null,                -- 'post' | 'comment' | 'user'
  board       text,                         -- 'tip' | 'free' (게시글/댓글일 때)
  target_id   text,                         -- 글/댓글 id 또는 대상 캐릭터명
  reason      text,
  status      text not null default 'open', -- open | resolved
  created_at  timestamptz not null default now()
);
create index if not exists idx_reports_status on reports(status);

-- 사용자 차단 (차단하면 상대 글/댓글이 내게 안 보임)
create table if not exists blocks (
  id          bigint generated always as identity primary key,
  blocker     text not null,
  blocked     text not null,
  created_at  timestamptz not null default now(),
  unique (blocker, blocked)
);
create index if not exists idx_blocks_blocker on blocks(blocker);
