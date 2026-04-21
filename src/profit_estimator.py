from __future__ import annotations

import math


def estimate_position_size(signal_score: int, annualized_volatility: float, structure: str) -> float:
    if 'No trade' in structure:
        return 0.0

    base = 0.02 if abs(signal_score) >= 4 else 0.01 if abs(signal_score) >= 3 else 0.005

    if annualized_volatility > 0.45:
        base *= 0.60
    elif annualized_volatility > 0.30:
        base *= 0.80

    return round(base * 100, 2)


def estimate_trade_outcome(
    last_close: float,
    annualized_volatility: float,
    signal_score: int,
    structure: str,
    days_to_expiration: int,
    strike_width: float | None = None,
) -> dict:
    days = max(days_to_expiration, 7)
    vol = max(float(annualized_volatility), 0.15)
    expected_move = last_close * vol * math.sqrt(days / 252)
    conviction_multiplier = 0.75 + min(abs(signal_score), 5) * 0.12
    projected_move = expected_move * conviction_multiplier

    if structure in ('Bull call spread', 'Bear put spread'):
        width = max(float(strike_width or round(last_close * 0.03, 0)), 2.0)
        debit = width * 100 * (0.35 + min(vol, 0.80) * 0.20)
        gross_profit_ceiling = width * 100
        projected_gross = min(gross_profit_ceiling, projected_move * 100 * 0.75)
        projected_profit = projected_gross - debit
        projected_roi = (projected_profit / debit) * 100 if debit else 0.0
        max_risk = debit
    elif structure in ('Long call', 'Long put'):
        debit = max(last_close * 0.012 * (1 + vol * 1.5) * 100, 120.0)
        projected_profit = projected_move * 100 - debit
        projected_roi = (projected_profit / debit) * 100 if debit else 0.0
        max_risk = debit
    else:
        debit = 0.0
        projected_profit = 0.0
        projected_roi = 0.0
        max_risk = 0.0

    return {
        'expected_move_usd': round(expected_move, 2),
        'estimated_cost_usd': round(debit, 2),
        'projected_profit_usd': round(projected_profit, 2),
        'projected_return_pct': round(projected_roi, 2),
        'max_risk_usd': round(max_risk, 2),
        'allocation_pct': estimate_position_size(signal_score, annualized_volatility, structure),
    }
