-- 박기백·박지은 결혼식 하객 업로드 사진
-- Supabase SQL Editor 에서 한 번만 실행
--
-- Storage bucket 도 함께 만들어야 함:
--   콘솔 → Storage → New bucket → name: wedding-photos → Public bucket ON

create table if not exists wedding_photos (
  id bigserial primary key,
  uploader_name text not null default '',
  uploader_uuid text not null default '',
  filename text not null,
  storage_path text not null,
  public_url text not null,
  file_size_bytes int not null default 0,
  width int not null default 0,
  height int not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists wedding_photos_created_idx
  on wedding_photos (created_at desc);

create index if not exists wedding_photos_uploader_idx
  on wedding_photos (uploader_uuid);
