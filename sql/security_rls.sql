-- 보안 강화: Row Level Security (RLS) 활성화
-- Supabase SQL Editor 에서 한 번 실행.
--
-- 백엔드(FastAPI)는 service_role 키로 접근하므로 RLS 를 우회한다 → 동작에 영향 없음.
-- RLS 를 켜고 정책을 만들지 않으면, anon/authenticated 역할(=공개 anon 키로 오는 요청)은
-- 해당 테이블에 '아무 행도' 접근하지 못한다(완전 차단). 민감 테이블에 필수.
--
-- ⚠️ 효과 확인: 켠 뒤 anon 키로 REST 조회가 빈 배열/권한오류가 나오면 정상.

-- 결혼식 사진 (하객 익명 업로드 — 직접 노출 차단)
alter table if exists wedding_photos enable row level security;

-- 회원 계정 (비밀번호 해시·이메일·생년월일 — 절대 직접 노출 금지)
alter table if exists users enable row level security;

-- 길드원/랭킹/게시판 등 그 외 테이블도 동일하게 권장:
alter table if exists members            enable row level security;
alter table if exists monthly_snapshots  enable row level security;
alter table if exists guild_contributions enable row level security;
alter table if exists notices            enable row level security;
alter table if exists tips               enable row level security;
alter table if exists tip_comments       enable row level security;
alter table if exists free_posts         enable row level security;
alter table if exists macro_comments     enable row level security;
alter table if exists visitors           enable row level security;
alter table if exists visit_stats        enable row level security;
alter table if exists rival_guilds       enable row level security;
alter table if exists rival_members      enable row level security;
alter table if exists rival_snapshots    enable row level security;

-- 참고: 위 테이블들은 모두 FastAPI(service_role)를 통해서만 읽고 쓰므로
-- 별도 policy 없이 RLS 만 켜도 기존 기능은 그대로 동작한다.
