-- 홈 개선 + 이력 추적 번들 — 2026-07-31
-- 쿠폰·홈 영상(admin 등록), 변경 이력(크롤 diff), 닉변 자동 감지.
-- 적용: Supabase SQL Editor에서 전체 실행 (한 번만).

-- 1) 쿠폰 (admin 등록 → 홈에 복사 버튼과 함께 표시)
create table if not exists coupons (
  id         bigint generated always as identity primary key,
  code       text not null unique,       -- 쿠폰 코드
  reward     text,                       -- 보상 설명 (선택)
  expires_at date,                       -- 만료일 (선택, 지난 건 홈에서 자동 숨김)
  active     boolean not null default true,
  created_at timestamptz not null default now()
);

-- 2) 홈 유튜브 영상 (admin이 URL 등록 → 홈 썸네일 섹션)
create table if not exists home_videos (
  id         bigint generated always as identity primary key,
  video_id   text not null unique,       -- 유튜브 영상 ID (URL에서 파싱)
  title      text,                       -- 표시 제목 (선택)
  created_at timestamptz not null default now()
);

-- 3) 변경 이력 (크롤 diff — 길드/직업/레벨/닉변)
create table if not exists change_log (
  id         bigint generated always as identity primary key,
  name       text not null,              -- 캐릭터명 (NFC)
  field      text not null,              -- guild | job | level | nickname
  old_value  text,
  new_value  text,
  changed_at timestamptz not null default now()
);
create index if not exists change_log_name_idx on change_log (name, changed_at desc);

-- 4) 닉변 의심 (크롤 diff 휴리스틱 → 운영진 확정 대기)
create table if not exists rename_suspects (
  id         bigint generated always as identity primary key,
  old_name   text not null,
  new_name   text not null,
  evidence   text,                       -- 판단 근거 (직업/레벨/전투력 일치 내용)
  status     text not null default 'pending',   -- pending | confirmed | dismissed
  created_at timestamptz not null default now(),
  unique (old_name, new_name)
);

-- 백엔드(service key)만 접근 — RLS 켜고 정책 없음
alter table coupons enable row level security;
alter table home_videos enable row level security;
alter table change_log enable row level security;
alter table rename_suspects enable row level security;
