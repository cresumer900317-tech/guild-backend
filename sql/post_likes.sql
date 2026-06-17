-- 게시글 좋아요 (유저별 1좋아요 + 토글) — 2026-06-17
-- 적용: Supabase SQL Editor에서 실행.
-- 기존 likes 카운트(익명 +1)는 그대로 두고, 로그인 유저의 좋아요만 여기 기록해 토글/중복방지.

create table if not exists post_likes (
  id             bigint generated always as identity primary key,
  board          text not null,             -- 'tip' | 'free'
  post_id        bigint not null,
  character_name text not null,
  created_at     timestamptz not null default now(),
  unique (board, post_id, character_name)
);
create index if not exists idx_post_likes_lookup on post_likes(board, post_id);
