"""아이템 비교 AI — 게임 아이템 비교 스크린샷을 Claude Vision 으로 판독·판정.

길드원들이 "좌측(장착중) vs 우측(비교 대상) 뭐가 좋아?" 하고 올리는 캡쳐를
자동으로 분석한다. 넥슨 공식 가이드(maplestoryidle.nexon.com/ko/guide)에
공개된 전투 공식·전투력 가중치를 프롬프트에 내장해 근거 있는 판정을 만든다.

ANTHROPIC_API_KEY 가 없으면 503 (ai.py 와 동일한 규칙).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from collections import deque

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

log = logging.getLogger("guild.item_compare")

try:
    from anthropic import Anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    Anthropic = None  # type: ignore[assignment,misc]
    _ANTHROPIC_AVAILABLE = False

router = APIRouter(prefix="/api/item-compare", tags=["item-compare"])

MAX_IMAGE_BYTES = 8 * 1024 * 1024

# ── 레이트리밋: IP당 분당 + 전체 일일 상한 (API 비용 방어) ──────
_HITS: dict[str, deque] = {}
_DAILY = {"day": "", "count": 0}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_ok(ip: str) -> bool:
    try:
        limit = int(os.environ.get("ITEM_COMPARE_RATE_PER_MIN", "6"))
    except ValueError:
        limit = 6
    if limit <= 0:
        return True
    now = time.monotonic()
    dq = _HITS.setdefault(ip, deque())
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def _daily_cap_ok() -> bool:
    try:
        cap = int(os.environ.get("ITEM_COMPARE_DAILY_CAP", "300"))
    except ValueError:
        cap = 300
    if cap <= 0:
        return True
    today = time.strftime("%Y-%m-%d")
    if _DAILY["day"] != today:
        _DAILY["day"] = today
        _DAILY["count"] = 0
    if _DAILY["count"] >= cap:
        return False
    _DAILY["count"] += 1
    return True


def _is_enabled() -> bool:
    return _ANTHROPIC_AVAILABLE and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _model() -> str:
    return os.environ.get("ITEM_COMPARE_MODEL", "claude-opus-5").strip() or "claude-opus-5"


def _media_type(content: bytes, filename: str) -> str:
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    raise HTTPException(status_code=400, detail="이미지 파일(PNG/JPG/WEBP)만 업로드할 수 있어요")


# ── 넥슨 공식 가이드 요약 (2026-08-06 공개분) — 판정 근거 ──────
_GAME_KNOWLEDGE = """\
[메이플 키우기 공식 전투 규칙 요약 — 넥슨 공식 가이드 기준]

1. 내부 계산은 1,000분율: 표시 30% = 계산상 300. (1+데미지) = (1,000+수치)÷1,000.
2. 능력치 누적 방식:
   - 최종 데미지만 효과별 곱연산 (20%+30% → 1.2×1.3 = +56%)
   - 데미지/데미지 증폭/보스·일반 몬스터 데미지/기본공격·스킬 데미지/스탯 비례 데미지 등 데미지류는 같은 종류끼리 합산
   - 받는 피해 감소·방어 관통력·쿨감%·공격 속도는 "체감 누적" (상한에 가까울수록 효율 감소. 상한: 받피감 95%, 방관 100%, 공속 150%, 쿨감% 100%)
3. 기본 전투 공식(요약): 피해 = 공격력 × 5,000÷(관통 적용 후 방어력+6,000) × (1+데미지) × (1+데미지 증폭) × (1+보스/일반 몬스터 데미지) × (1+기본공격/스킬 데미지) × (1+스탯 비례 데미지) × 크리티컬 데미지 × 최소~최대 데미지 배율 × (1+상태이상 데미지) × 최종 데미지 × 콘텐츠 보정 × 스킬 계수
4. 전투력 공식 기본항: (공격력×3 + 최대HP×0.05 + 방어력×0.2), 이후 능력치별 가중치 곱연산(백만분율):
   - 데미지·공격 속도·스탯 비례 데미지·최종 데미지·데미지 증폭: ×700
   - 방어 관통력: ×1,000 / 받는 피해 감소: ×200
   - 크리티컬 확률 ×350, 크리티컬 데미지(-300) ×550
   - 최대/최소 데미지 배율(기준 초과분) ×350
   - 일반/보스 몬스터 데미지 ×350, 상태이상 데미지 ×350
   - 스킬 데미지 ×200, 기본 공격 데미지 ×500
   - 스킬 레벨(레벨당): 1차 ×500, 2차 ×1,000, 3차 ×2,500, 4차 ×3,500, 모든 스킬 ×6,000
   - 버프 지속·동료 소환 지속 ×250, 쿨감(초) ×25, 쿨감(%) ×250, 기본 공격 대상 수 ×30,000
   - 명중 ×4,500, 회피 ×1,000 (수치당)
   - 최대 HP ×1, 방어력 ×20, 최대 MP(-500) ×10
5. 콘텐츠별 특성:
   - 보스/길드 레이드: 보스 몬스터 데미지 유효, 제곱근 피해 조정. 지속 딜 중요 → 스킬 데미지·쿨감 가치↑
   - 사냥(스테이지): 일반 몬스터 데미지·기본 공격 대상 수·공격 속도 가치↑
   - PVP(아레나·콜로세움): 보스/일반 몬스터 데미지 무효. 공/방 능력치와 최대 HP·받피감이 보정에 반영. 레벨 보정 존재
6. 전투력은 참고 지표일 뿐, 콘텐츠별 실전 가치와 다를 수 있음 (예: 보스 데미지는 전투력 가중치가 낮지만 보스전 실전 가치는 높음)."""

_SYSTEM = f"""당신은 모바일 게임 '메이플 키우기'의 아이템 비교 전문가입니다.
사용자가 올린 게임 내 아이템 비교 스크린샷을 판독하고, 어느 쪽이 좋은지 판정합니다.

{_GAME_KNOWLEDGE}

[스크린샷 판독 요령]
- 보통 왼쪽 패널이 장착 중("장착중" 표기), 오른쪽 패널이 비교 대상(장착/분해 버튼)입니다. 표기가 다르면 화면 기준으로 판단하세요.
- 오른쪽 패널의 초록색 숫자는 (오른쪽-왼쪽) 스탯 차이입니다. "전투력 변화" 값이 보이면 그대로 읽으세요.
- 등급(하급/중급/상급/전설 등)·레벨·별 강화 수치도 읽으세요.
- 숫자는 천 단위 구분 쉼표를 제거하고 정확히 읽으세요. 확실하지 않은 값은 null 로 두세요.

[판정 원칙]
- 전투력 공식 가중치로 정량 비교하되, 콘텐츠별 실전 가치(보스/사냥/PVP)를 반드시 구분해 판정하세요.
- 옵션 종류가 다를 때(예: 2차 스킬 레벨 vs 4차 스킬 레벨)는 가중치 표로 환산해 비교 근거를 제시하세요.
- 요약(summary)은 길드원에게 말하듯 한국어로 2~4문장, 결론부터 명확하게.
- 스크린샷이 아이템 비교 화면이 아니면 parse_ok=false 로 하고 error 에 이유를 적으세요."""

_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parse_ok", "error", "left", "right", "verdict"],
    "properties": {
        "parse_ok": {"type": "boolean"},
        "error": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "left": {"$ref": "#/$defs/item"},
        "right": {"$ref": "#/$defs/item"},
        "verdict": {
            "type": "object",
            "additionalProperties": False,
            "required": ["winner", "confidence", "power_change_read", "by_content", "summary", "caution"],
            "properties": {
                "winner": {"type": "string", "enum": ["left", "right", "tie", "depends", "unknown"]},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "power_change_read": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "by_content": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["content", "winner", "reason"],
                        "properties": {
                            "content": {"type": "string"},
                            "winner": {"type": "string", "enum": ["left", "right", "tie", "unknown"]},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "summary": {"type": "string"},
                "caution": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
        },
    },
    "$defs": {
        "item": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "grade", "level", "equipped", "stats"],
            "properties": {
                "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "grade": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "level": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "equipped": {"type": "boolean"},
                "stats": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["stat", "value", "diff"],
                        "properties": {
                            "stat": {"type": "string"},
                            "value": {"type": "string"},
                            "diff": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        },
                    },
                },
            },
        },
    },
}


@router.post("/analyze")
async def analyze(request: Request, file: UploadFile = File(...)):
    if not _is_enabled():
        raise HTTPException(status_code=503, detail="AI 분석 기능이 아직 설정되지 않았어요")
    if not _rate_limit_ok(_client_ip(request)):
        raise HTTPException(status_code=429, detail="요청이 너무 많아요. 잠시 후 다시 시도해 주세요")
    if not _daily_cap_ok():
        raise HTTPException(status_code=429, detail="오늘 분석 한도를 모두 사용했어요. 내일 다시 시도해 주세요")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="빈 파일입니다")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="이미지가 너무 큽니다 (8MB 이하)")
    media_type = _media_type(content, file.filename or "")

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
    model = _model()
    started = time.monotonic()
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_SYSTEM,
            # effort=medium: 판독+판정엔 충분하고 사고 토큰(=출력 과금)을 절감
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(content).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": "이 아이템 비교 스크린샷을 분석해서 어느 쪽이 좋은지 판정해줘."},
                ],
            }],
        )
    except Exception as e:  # noqa: BLE001 — 모델/네트워크 오류는 502 로 뭉뚱그림
        log.error("item_compare model call failed: %s", e)
        raise HTTPException(status_code=502, detail="AI 분석 호출에 실패했어요. 잠시 후 다시 시도해 주세요")

    if getattr(msg, "stop_reason", None) == "refusal":
        raise HTTPException(status_code=422, detail="이 이미지는 분석할 수 없어요")

    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        log.error("item_compare bad json: %.300s", text)
        raise HTTPException(status_code=502, detail="AI 응답 해석에 실패했어요. 다시 시도해 주세요")

    usage = getattr(msg, "usage", None)
    if usage:
        log.info(
            "item_compare ok model=%s input=%s output=%s elapsed=%.1fs",
            model, getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"), time.monotonic() - started,
        )
    data["meta"] = {"model": model, "elapsed_sec": round(time.monotonic() - started, 1)}
    return data
