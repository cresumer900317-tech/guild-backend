"""Claude API 단가 + 월 한도 설정.

단일 위치에서 단가를 관리한다. 모델 변경 시 이 파일만 수정.
2026-05 기준 claude-haiku-4-5-20251001 정가:
  - input:  $1.00 / 1M tokens
  - output: $5.00 / 1M tokens
"""
from __future__ import annotations
import os

PRICE_INPUT_PER_M_USD: float = 1.0
PRICE_OUTPUT_PER_M_USD: float = 5.0


def calc_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """입력/출력 토큰 → 추정 USD. 6자리 반올림."""
    in_t = max(0, int(input_tokens or 0))
    out_t = max(0, int(output_tokens or 0))
    cost = (in_t * PRICE_INPUT_PER_M_USD + out_t * PRICE_OUTPUT_PER_M_USD) / 1_000_000.0
    return round(cost, 6)


def monthly_budget_usd() -> float:
    """환경변수 AI_MONTHLY_BUDGET_USD (default 10.00).

    Anthropic 콘솔에서 설정한 한도와 일치시켜야 의미가 있다.
    """
    raw = os.environ.get("AI_MONTHLY_BUDGET_USD", "10.00").strip()
    try:
        v = float(raw)
        if v <= 0:
            return 10.00
        return v
    except ValueError:
        return 10.00
