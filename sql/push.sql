-- 푸시 알림 (길드라운지 앱) — 2026-06-17
-- 적용: Supabase SQL Editor에서 실행.

-- ── 기기 토큰 (로그인 시 앱이 등록, 로그아웃/거부 시 삭제) ──────────
create table if not exists push_tokens (
  id            bigint generated always as identity primary key,
  character_name text not null,
  token         text not null unique,        -- ExponentPushToken[...]
  platform      text,                         -- ios | android
  created_at    timestamptz not null default now()
);

-- ── 발송 로그 (중복 발송 방지) ────────────────────────────────────
-- occurrence_key: 시즌=occurrence id(int 문자열), 상시=합성키("tobeol@<iso>")
-- kind: start | last_day | end_3h | end_1h
create table if not exists push_log (
  id            bigint generated always as identity primary key,
  occurrence_key text not null,
  kind          text not null,
  created_at    timestamptz not null default now(),
  unique (occurrence_key, kind)
);
