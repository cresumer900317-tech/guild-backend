-- 만수로그: 반려묘 박만수 몸무게 기록 (jisoar.com/mansu)
-- Supabase SQL Editor 에서 한 번 실행 (멱등)
create table if not exists mansu_log (
  id bigint generated always as identity primary key,
  date date not null unique,
  grams integer not null check (grams between 1 and 20000),
  memo text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- 다른 개인 테이블과 동일하게 RLS 켜두기 (백엔드는 service key 라 영향 없음)
alter table mansu_log enable row level security;
