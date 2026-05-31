"""박기백·박지은 결혼식 하객 사진 업로드/갤러리.

- 누구나 (로그인 없이) 사진 업로드 가능
- 갤러리/삭제는 WEDDING_ADMIN_TOKEN 으로만 접근
"""
from __future__ import annotations

import io
import os
import secrets
import time
import urllib.parse
import zipfile
from collections import deque
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from database import supabase


router = APIRouter(prefix="/api/wedding", tags=["wedding"])


# ── 설정 ──────────────────────────────────────────────
def _admin_token() -> str:
    # 하드코딩 기본값 없음 — 환경변수 WEDDING_ADMIN_TOKEN 미설정 시 관리자 접근 전면 차단(fail-closed).
    return os.environ.get("WEDDING_ADMIN_TOKEN", "").strip()


# ── 업로드 레이트리밋 (스크립트 대량 업로드 방어) ──────
# IP당 분당 허용 수. 0 이면 비활성. 예식장 단일 NAT(많은 하객이 같은 공인 IP)를
# 고려해 기본값을 넉넉히. 하객이 막히면 값을 올리거나 0으로.
_UPLOAD_HITS: dict[str, deque] = {}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_ok(ip: str) -> bool:
    try:
        limit = int(os.environ.get("WEDDING_UPLOAD_RATE_PER_MIN", "600"))
    except ValueError:
        limit = 600
    if limit <= 0:
        return True
    now = time.monotonic()
    dq = _UPLOAD_HITS.setdefault(ip, deque())
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def _bucket_name() -> str:
    return os.environ.get("SUPABASE_STORAGE_BUCKET_WEDDING", "wedding-photos").strip() or "wedding-photos"


def _supabase_creds() -> tuple[str, str]:
    sb_url = os.environ.get("SUPABASE_URL", "").strip()
    sb_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not sb_url or not sb_key:
        raise HTTPException(status_code=500, detail="Supabase 설정이 안 되어 있습니다")
    return sb_url, sb_key


def _check_admin(key: Optional[str]):
    tok = _admin_token()
    if not tok or not key or key.strip() != tok:
        raise HTTPException(status_code=403, detail="비공개 페이지입니다")


# ── 부팅 시 테이블 자동 생성 ──────────────────────────
def ensure_table():
    """wedding_photos 테이블이 없으면 빈 SELECT 한 번으로 존재 체크만.

    실제 생성은 sql/wedding.sql 을 한 번 돌려야 한다 (이건 멱등).
    """
    try:
        supabase.table("wedding_photos").select("id").limit(1).execute()
    except Exception:
        # 테이블이 아직 없을 수도 있음. 첫 업로드 때 오류로 알아챌 수 있게 그대로 두기.
        pass


# ── 업로드 ────────────────────────────────────────────
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")
ALLOWED_EXTS = IMAGE_EXTS + VIDEO_EXTS

# 확장자 → content-type (브라우저가 type 을 안 보낼 때 fallback)
_CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic", ".heif": "image/heif",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/x-m4v",
}


def _max_upload_mb() -> int:
    try:
        return int(os.environ.get("WEDDING_MAX_UPLOAD_MB", "100"))
    except ValueError:
        return 100


def _guess_content_type(filename: str) -> str:
    ext = (os.path.splitext(filename or "")[1] or "").lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _gen_filename(orig_name: str) -> str:
    ext = (os.path.splitext(orig_name or "")[1] or ".jpg").lower()
    if ext not in ALLOWED_EXTS:
        ext = ".jpg"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rand = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]
    return f"{ts}-{rand}{ext}"


@router.post("/upload")
async def upload_photo(
    request: Request,
    file: UploadFile = File(...),
    uploader_name: str = Form(""),
    uploader_uuid: str = Form(""),
    width: int = Form(0),
    height: int = Form(0),
):
    """누구나 사진 업로드 (인증 없음). IP당 분당 한도로 대량 업로드 방어."""
    if not _rate_limit_ok(_client_ip(request)):
        raise HTTPException(status_code=429, detail="업로드 요청이 너무 많아요. 잠시 후 다시 시도해 주세요")
    if not file:
        raise HTTPException(status_code=400, detail="파일이 없습니다")

    content = await file.read()
    size = len(content)
    if size <= 0:
        raise HTTPException(status_code=400, detail="빈 파일입니다")
    max_mb = _max_upload_mb()
    if size > max_mb * 1024 * 1024:  # 사진은 클라이언트 압축됨, 동영상은 원본 → 넉넉히
        raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다 ({max_mb}MB 이하)")

    filename = _gen_filename(file.filename or "")
    storage_path = filename  # bucket 루트에 평탄하게 저장

    sb_url, sb_key = _supabase_creds()
    bucket = _bucket_name()

    # Storage 업로드 (REST API 직접 호출 — supabase-py 의 storage 인터페이스가 환경마다 차이가 있어 안전하게)
    # content-type 은 클라이언트가 보낸 값을 믿지 않고 '정제된 확장자'에서만 도출.
    # (안 그러면 text/html 등으로 공개 버킷에 임의 웹페이지/스크립트를 호스팅하는 악용 가능)
    content_type = _guess_content_type(filename)
    storage_upload_url = f"{sb_url}/storage/v1/object/{bucket}/{urllib.parse.quote(storage_path)}"
    try:
        with httpx.Client(timeout=60) as client:
            up_resp = client.post(
                storage_upload_url,
                content=content,
                headers={
                    "apikey": sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Content-Type": content_type,
                    "x-upsert": "false",
                },
            )
        if up_resp.status_code >= 300:
            raise HTTPException(
                status_code=502,
                detail=f"Storage 업로드 실패 ({up_resp.status_code}): {up_resp.text[:200]}",
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Storage 통신 실패: {e}")

    public_url = f"{sb_url}/storage/v1/object/public/{bucket}/{urllib.parse.quote(storage_path)}"

    row = {
        "uploader_name": (uploader_name or "").strip()[:40],
        "uploader_uuid": (uploader_uuid or "").strip()[:64],
        "filename": filename,
        "storage_path": storage_path,
        "public_url": public_url,
        "file_size_bytes": size,
        "width": max(0, int(width or 0)),
        "height": max(0, int(height or 0)),
    }
    try:
        result = supabase.table("wedding_photos").insert(row).execute()
    except Exception as e:
        # DB 기록 실패해도 storage 는 이미 올라간 상태. 사용자에겐 일단 OK
        # 로그 남기고 그대로 종료
        print(f"[wedding] DB insert failed: {e}")
        return {"status": "ok", "id": None, "url": public_url, "warning": "DB 기록 실패"}

    inserted = result.data[0] if result.data else {}
    return {
        "status": "ok",
        "id": inserted.get("id"),
        "url": public_url,
        "filename": filename,
    }


# ── 딥 헬스체크 (모니터링용, 인증 없음) ────────────────
# GET + HEAD 둘 다 허용: UptimeRobot 무료 플랜은 기본 HEAD 로 찔러보는데
# GET 전용이면 405(Method Not Allowed)가 떠서 멀쩡한데도 '다운'으로 오인됨.
@router.api_route("/health", methods=["GET", "HEAD"])
def deep_health():
    """업로드 경로의 핵심 의존성(DB)까지 실제로 확인.

    - DB 연결 OK  → 200 {"ok": true}
    - DB 연결 실패 → 503 {"ok": false}  (UptimeRobot 등이 '다운'으로 감지)
    얕은 /healthz 와 달리 Supabase 가 죽으면 여기서 503 이 떠서 알림이 의미를 가짐.
    """
    try:
        supabase.table("wedding_photos").select("id").limit(1).execute()
        return JSONResponse(status_code=200, content={"ok": True, "db": True, "service": "wedding"})
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "db": False, "service": "wedding", "error": str(e)[:120]},
        )


# ── 공개 카운트 (라이브 카운터용, 인증 없음) ───────────
@router.get("/count")
def count_photos():
    """모인 사진/영상 수 + 참여자(고유 uuid) 수. 숫자만 노출."""
    try:
        result = supabase.table("wedding_photos").select("filename, uploader_uuid").execute()
        rows = result.data or []
    except Exception:
        return {"total": 0, "images": 0, "videos": 0, "contributors": 0}
    videos = 0
    for r in rows:
        ext = (os.path.splitext(r.get("filename") or "")[1] or "").lower()
        if ext in VIDEO_EXTS:
            videos += 1
    uuids = {(r.get("uploader_uuid") or "").strip() for r in rows}
    uuids.discard("")
    return {
        "total": len(rows),
        "images": len(rows) - videos,
        "videos": videos,
        "contributors": len(uuids),
    }


# ── 갤러리 (관리자) ────────────────────────────────────
@router.get("/list")
def list_photos(key: str = Query("")):
    _check_admin(key)
    try:
        result = supabase.table("wedding_photos") \
            .select("*") \
            .order("created_at", desc=True) \
            .execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"목록 조회 실패: {e}")
    rows = result.data or []
    return {
        "total": len(rows),
        "photos": rows,
    }


@router.delete("/{photo_id}")
def delete_photo(photo_id: int, key: str = Query(""), uuid: str = Query("")):
    """사진 삭제.

    인증은 둘 중 하나:
      - admin 토큰(key) — 신랑·신부 갤러리
      - 본인 업로더 uuid(uuid) — 하객이 방금 올린 자기 사진 '회수'
    """
    result = supabase.table("wedding_photos").select("*").eq("id", photo_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="없는 사진")

    photo = result.data[0]

    # 권한 확인: admin 토큰 또는 본인 uuid 일치
    is_admin = bool(key) and key.strip() == _admin_token()
    owner_uuid = (photo.get("uploader_uuid") or "").strip()
    is_owner = bool(uuid) and owner_uuid != "" and uuid.strip() == owner_uuid
    if not (is_admin or is_owner):
        raise HTTPException(status_code=403, detail="삭제 권한이 없습니다")
    storage_path = photo.get("storage_path") or photo.get("filename")
    sb_url, sb_key = _supabase_creds()
    bucket = _bucket_name()

    # Storage 에서도 제거 (실패해도 DB row 는 지움)
    try:
        with httpx.Client(timeout=30) as client:
            client.request(
                "DELETE",
                f"{sb_url}/storage/v1/object/{bucket}/{urllib.parse.quote(storage_path)}",
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            )
    except Exception as e:
        print(f"[wedding] storage delete failed (id={photo_id}): {e}")

    supabase.table("wedding_photos").delete().eq("id", photo_id).execute()
    return {"status": "ok"}


@router.get("/zip")
def download_zip(key: str = Query("")):
    """전체 사진 ZIP 스트림 다운로드."""
    _check_admin(key)

    result = supabase.table("wedding_photos") \
        .select("filename, public_url, uploader_name, created_at") \
        .order("created_at", desc=False) \
        .execute()
    photos = result.data or []
    if not photos:
        raise HTTPException(status_code=404, detail="사진이 없습니다")

    def iter_zip():
        # 전체 ZIP 을 메모리에 빌드 후 청크 단위로 스트림.
        # ZIP_STORED — 이미지는 이미 압축돼 있으므로 무압축이 빠르고 CPU 절약.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as zf:
            with httpx.Client(timeout=60) as client:
                for idx, p in enumerate(photos, 1):
                    url = p.get("public_url")
                    if not url:
                        continue
                    name = p.get("filename") or f"photo-{idx}.jpg"
                    uploader = (p.get("uploader_name") or "").strip()
                    prefix = f"{uploader}_" if uploader else ""
                    arcname = f"{idx:04d}_{prefix}{name}"
                    try:
                        r = client.get(url)
                        if r.status_code != 200:
                            continue
                        zf.writestr(arcname, r.content)
                    except Exception as e:
                        print(f"[wedding] zip fetch failed {url}: {e}")
                        continue
        buffer.seek(0)
        while True:
            chunk = buffer.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    ts = datetime.now().strftime("%Y%m%d")
    fname = f"wedding-photos-{ts}.zip"
    return StreamingResponse(
        iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=\"{fname}\""},
    )
