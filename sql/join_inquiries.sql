-- 길드 가입 문의 (길드라운지 앱) — 2026-06-21
-- 비로그인 외부인이 앱에서 가입 문의를 남기면 여기 저장 → 운영진이 앱에서 검토.
-- 적용: Supabase SQL Editor에서 실행.

create table if not exists join_inquiries (
  id             bigint generated always as identity primary key,
  character_name text not null,                       -- 문의자 캐릭터명/닉네임
  power_text     text,                                -- 전투력·레벨 등 자유 입력(선택)
  contact        text,                                -- 연락처: 오픈톡 닉/디스코드/카톡(선택)
  message        text,                                -- 하고 싶은 말(선택)
  status         text not null default 'pending',     -- pending | accepted | rejected
  created_at     timestamptz not null default now()
);

create index if not exists join_inquiries_status_idx on join_inquiries (status, created_at desc);
