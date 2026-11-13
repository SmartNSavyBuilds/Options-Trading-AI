from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class GuardDecision:
    code: str = ''
    detail: str = ''


def evaluate_entry(
    reference_close: float,
    midpoint: float,
    option_spread: float,
    earnings_dates: list[date],
    headline_count: int,
    iv_rank: float,
    as_of: date,
    max_drift: float = 0.05,
    max_spread: float = 0.06,
    blackout_days: int = 7,
    news_threshold: int = 3,
    news_move: float = 0.04,
    max_iv_rank: float = 80.0,
) -> GuardDecision:
    numbers = (reference_close, midpoint, option_spread, iv_rank, max_drift, max_spread, news_move, max_iv_rank)
    if not all(math.isfinite(value) for value in numbers):
        return GuardDecision('REJECTED_SAFETY_DATA_UNAVAILABLE', 'A required safety value is missing or non-finite.')
    if reference_close <= 0 or midpoint <= 0 or option_spread < 0 or headline_count < 0 or blackout_days < 0 or news_threshold <= 0:
        return GuardDecision('REJECTED_SAFETY_DATA_UNAVAILABLE', 'A required safety value is invalid.')
    if max_drift < 0 or max_spread < 0 or news_move < 0 or not 0 <= max_iv_rank <= 100:
        return GuardDecision('REJECTED_SAFETY_DATA_UNAVAILABLE', 'Safety thresholds are invalid.')
    drift = (midpoint - reference_close) / reference_close
    if abs(drift) > max_drift:
        return GuardDecision('REJECTED_INTRADAY_DRIFT', f'Intraday drift {drift * 100:.2f}% exceeds {max_drift * 100:.2f}%.')
    if option_spread > max_spread:
        return GuardDecision('REJECTED_SPREAD_WIDTH', f'Option spread {option_spread * 100:.2f}% exceeds {max_spread * 100:.2f}%.')
    upcoming = [event for event in earnings_dates if 0 <= (event - as_of).days <= blackout_days]
    if upcoming:
        return GuardDecision('REJECTED_CATALYST_BLACKOUT', f'MANUAL_REVIEW_REQUIRED: earnings on {min(upcoming).isoformat()}.')
    if headline_count >= news_threshold and abs(drift) >= news_move:
        return GuardDecision('REJECTED_CATALYST_BLACKOUT', f'MANUAL_REVIEW_REQUIRED: {headline_count} headlines and {drift * 100:.2f}% move.')
    if iv_rank > max_iv_rank:
        return GuardDecision('REJECTED_IV_RANK', f'IV rank {iv_rank:.2f} exceeds {max_iv_rank:.2f}.')
    return GuardDecision()