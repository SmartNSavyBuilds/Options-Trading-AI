from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Dict, List

import pandas as pd
import yfinance as yf

from .multi_asset import build_market_regime_summary
from .profit_estimator import estimate_trade_outcome
from .market_data import stock_quote_midpoint


OUTPUT_DIR = Path(__file__).resolve().parent.parent / 'outputs'


def _build_candidate_refinement(
    market_regime: str,
    bias: str,
    annualized_volatility: float,
    days_to_expiration: int,
    estimates: dict[str, float],
) -> tuple[float, str, str]:
    adjustment = 0.0
    alignment = 'neutral'
    notes: list[str] = []

    regime_key = str(market_regime or 'unknown').strip().lower()
    bias_key = str(bias or 'neutral').strip().lower()

    if regime_key == 'risk_on':
        if bias_key == 'bullish':
            adjustment += 6.0
            alignment = 'tailwind'
            notes.append('Risk on backdrop supports bullish continuation setups.')
        elif bias_key == 'bearish':
            adjustment -= 6.0
            alignment = 'headwind'
            notes.append('Risk on backdrop works against bearish timing.')
        else:
            notes.append('Risk on tape still favors selectivity for neutral ideas.')
    elif regime_key == 'risk_off':
        if bias_key == 'bearish':
            adjustment += 6.0
            alignment = 'tailwind'
            notes.append('Risk off backdrop favors defensive or bearish exposure.')
        elif bias_key == 'bullish':
            adjustment -= 6.0
            alignment = 'headwind'
            notes.append('Risk off backdrop argues for smaller bullish exposure.')
        else:
            notes.append('Defensive tape keeps neutral ideas on watch only.')
    else:
        notes.append('Mixed regime keeps the ranking selective.')

    if 0.18 <= annualized_volatility <= 0.55:
        adjustment += 2.0
        notes.append('Volatility is in a healthier range for defined risk trades.')
    elif annualized_volatility > 0.80:
        adjustment -= 4.0
        notes.append('Volatility is elevated, so the setup is penalized.')
    elif 0 < annualized_volatility < 0.12 and bias_key != 'neutral':
        adjustment -= 1.5
        notes.append('Muted volatility may slow the option payoff window.')

    if bias_key != 'neutral':
        if days_to_expiration < 14:
            adjustment -= 2.5
            notes.append('The expiry is short dated, leaving less time for the thesis to work.')
        elif 21 <= days_to_expiration <= 45:
            adjustment += 1.5
            notes.append('The expiry window fits the intended swing horizon.')

    max_risk_usd = float(estimates.get('max_risk_usd', 0.0) or 0.0)
    projected_profit_usd = float(estimates.get('projected_profit_usd', 0.0) or 0.0)
    reward_to_risk = projected_profit_usd / max(max_risk_usd, 1.0)
    if reward_to_risk >= 0.8:
        adjustment += 2.0
        notes.append('Projected reward to risk is attractive.')
    elif reward_to_risk < 0.35 and bias_key != 'neutral':
        adjustment -= 2.0
        notes.append('Projected reward to risk is modest, so the idea is de-emphasized.')

    return round(adjustment, 2), alignment, ' '.join(notes)


def _load_learning_feedback(learning_feedback: pd.DataFrame | None = None) -> pd.DataFrame:
    if learning_feedback is not None:
        return learning_feedback.copy()

    matches = sorted(OUTPUT_DIR.glob('learning_feedback*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        return pd.DataFrame()

    try:
        return pd.read_csv(matches[0])
    except Exception:
        return pd.DataFrame()


def _lookup_learning_feedback(ticker: str, learning_feedback: pd.DataFrame) -> tuple[float, str]:
    if learning_feedback.empty:
        return 0.0, 'No recent paper trade feedback yet.'

    working = learning_feedback.copy()
    if 'ticker' not in working.columns:
        return 0.0, 'No recent paper trade feedback yet.'

    working['ticker'] = working['ticker'].astype(str).str.upper()
    lookup_ticker = str(ticker or '').upper()

    notes: list[str] = []
    total_adjustment = 0.0

    portfolio_row = working.loc[working['ticker'].eq('__PORTFOLIO__')]
    if not portfolio_row.empty:
        total_adjustment += float(portfolio_row.iloc[0].get('learning_adjustment', 0.0) or 0.0)
        portfolio_note = str(portfolio_row.iloc[0].get('learning_note', '')).strip()
        if portfolio_note:
            notes.append(portfolio_note)

    ticker_row = working.loc[working['ticker'].eq(lookup_ticker)]
    if not ticker_row.empty:
        total_adjustment += float(ticker_row.iloc[0].get('learning_adjustment', 0.0) or 0.0)
        ticker_note = str(ticker_row.iloc[0].get('learning_note', '')).strip()
        if ticker_note:
            notes.append(ticker_note)

    if not notes:
        notes.append('No recent paper trade feedback yet.')

    total_adjustment = max(min(total_adjustment, 8.0), -8.0)
    return round(total_adjustment, 2), ' '.join(notes)


def _nearest_expiration(expirations: List[str], min_days: int = 21, max_days: int = 60) -> str | None:
    if not expirations:
        return None

    candidates: List[tuple[str, int]] = []
    for exp in expirations:
        try:
            days = (pd.Timestamp(exp) - pd.Timestamp.today().normalize()).days
        except Exception:
            continue
        if days >= 0:
            candidates.append((exp, int(days)))

    if not candidates:
        return None

    target_days = (min_days + max_days) // 2
    within_range = [item for item in candidates if min_days <= item[1] <= max_days]
    if not within_range:
        return None

    return min(within_range, key=lambda item: (abs(item[1] - target_days), item[1]))[0]


def _pick_spread_strikes(last_close: float, bias: str, signal_score: int) -> tuple[float, float]:
    width_pct = 0.03 if abs(signal_score) >= 4 else 0.02
    if bias == 'bullish':
        long_strike = round(last_close * 1.00, 0)
        short_strike = round(last_close * (1.00 + width_pct), 0)
    elif bias == 'bearish':
        long_strike = round(last_close * 1.00, 0)
        short_strike = round(last_close * (1.00 - width_pct), 0)
    else:
        long_strike = round(last_close, 0)
        short_strike = round(last_close, 0)
    return long_strike, short_strike


def build_options_candidates(signal_report: pd.DataFrame, learning_feedback: pd.DataFrame | None = None, quote_client=None) -> pd.DataFrame:
    from .execution import TradingConfig, _intraday_drift

    if quote_client is None:
        from alpaca.data.historical import StockHistoricalDataClient

        config = TradingConfig.from_env()
        if config.api_key and config.secret_key:
            quote_client = StockHistoricalDataClient(config.api_key, config.secret_key)
    max_drift = float(os.getenv('MAX_INTRADAY_DRIFT_PCT', '0.05'))
    quote_max_age = float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60'))
    rows: List[Dict[str, object]] = []
    regime_summary = build_market_regime_summary(signal_report)
    market_regime = str(regime_summary.iloc[0].get('market_regime', 'unknown')) if not regime_summary.empty else 'unknown'
    learning_feedback = _load_learning_feedback(learning_feedback)

    for _, row in signal_report.iterrows():
        ticker = row['ticker']
        bias = row['bias']
        structure = row['suggested_structure']
        raw_close = pd.to_numeric(row.get('last_close', 0), errors='coerce')
        last_close = float(raw_close) if pd.notna(raw_close) and math.isfinite(float(raw_close)) else 0.0
        signal_score = int(row.get('signal_score', 0))
        annualized_volatility = float(row.get('annualized_volatility', 0.0))

        expiration = None
        days_to_expiration = 30
        try:
            tk = yf.Ticker(ticker)
            expiration = _nearest_expiration(
                list(tk.options), max_days=45 if structure in {'Bull call spread', 'Bear put spread'} else 60,
            )
            if expiration:
                days_to_expiration = max((pd.Timestamp(expiration) - pd.Timestamp.today().normalize()).days, 1)
        except Exception:
            expiration = None

        midpoint = None
        drift = None
        rejection_code = ''
        rejection_detail = ''
        try:
            if not math.isfinite(max_drift) or max_drift < 0:
                raise ValueError('Invalid intraday drift threshold')
            if quote_client is None:
                raise ValueError('Stock market data credentials unavailable')
            midpoint = stock_quote_midpoint(quote_client, ticker, quote_max_age)
            drift = _intraday_drift(last_close, midpoint)
            if drift is None:
                raise ValueError('Signal reference price unavailable')
            if abs(drift) > max_drift:
                rejection_code = 'REJECTED_INTRADAY_DRIFT'
                rejection_detail = f'Intraday drift {drift * 100:.2f}% exceeds {max_drift * 100:.2f}%.'
        except Exception as exc:
            rejection_code = 'REJECTED_SAFETY_DATA_UNAVAILABLE'
            rejection_detail = f'Stock quote unavailable or invalid ({type(exc).__name__}).'

        long_strike, short_strike = _pick_spread_strikes(midpoint if midpoint is not None else last_close, bias, signal_score)
        strike_width = abs(short_strike - long_strike)
        spread_type = {'Bull call spread': 'bull_call_debit', 'Bear put spread': 'bear_put_debit'}.get(structure, '')
        if spread_type and not rejection_code:
            if not expiration:
                rejection_code = 'REJECTED_SPREAD_EXPIRATION'
                rejection_detail = 'No eligible 21-45 day expiration is available.'
            elif long_strike <= 0 or short_strike <= 0 or long_strike == short_strike:
                rejection_code = 'REJECTED_SPREAD_STRIKES'
                rejection_detail = 'A vertical spread requires two distinct positive strikes.'
            elif (spread_type == 'bull_call_debit' and (bias != 'bullish' or long_strike >= short_strike)) or (
                spread_type == 'bear_put_debit' and (bias != 'bearish' or long_strike <= short_strike)
            ):
                rejection_code = 'REJECTED_SPREAD_STRIKES'
                rejection_detail = 'Spread strike order or direction is invalid.'

        if structure == 'Bull call spread':
            setup = f'Buy {int(long_strike)}C / Sell {int(short_strike)}C'
        elif structure == 'Bear put spread':
            setup = f'Buy {int(long_strike)}P / Sell {int(short_strike)}P'
        elif structure == 'Long call':
            setup = f'Buy {int(long_strike)}C'
        elif structure == 'Long put':
            setup = f'Buy {int(long_strike)}P'
        else:
            setup = 'No trade'
        if rejection_code:
            setup = 'No trade'

        estimates = estimate_trade_outcome(
            last_close=last_close,
            annualized_volatility=annualized_volatility,
            signal_score=signal_score,
            structure=structure,
            days_to_expiration=days_to_expiration,
            strike_width=strike_width,
        )

        setup_quality = int(row.get('setup_quality', abs(signal_score)) or 0)
        long_rank_score = float(row.get('long_rank_score', 0.0) or 0.0)
        short_rank_score = float(row.get('short_rank_score', 0.0) or 0.0)
        conviction_rank = max(long_rank_score, short_rank_score)
        base_rank_score = round(conviction_rank + setup_quality * 5 + max(estimates['projected_return_pct'], -50) * 0.15, 2)
        refinement_adjustment, regime_alignment, refinement_note = _build_candidate_refinement(
            market_regime=market_regime,
            bias=bias,
            annualized_volatility=annualized_volatility,
            days_to_expiration=days_to_expiration,
            estimates=estimates,
        )
        learning_adjustment, learning_note = _lookup_learning_feedback(ticker, learning_feedback)
        rank_score = round(base_rank_score + refinement_adjustment + learning_adjustment, 2)

        trade_action = 'call-based bullish exposure' if structure in {'Bull call spread', 'Long call'} else 'put-based bearish exposure' if structure in {'Bear put spread', 'Long put'} else 'stand aside'
        advisor_note = row.get('advisor_summary', 'No advisor summary available yet.')
        sell_plan = row.get('sell_plan', 'Exit when the thesis changes or the risk limit is hit.')
        source = row.get('opportunity_source', 'price_action_only')
        thesis_strength = row.get('thesis_strength', 'developing')

        take_profit_pct = 25.0 if structure in {'Long call', 'Long put'} else 18.0 if structure in {'Bull call spread', 'Bear put spread'} else 0.0
        stop_loss_pct = 12.0 if structure in {'Long call', 'Long put'} else 8.0 if structure in {'Bull call spread', 'Bear put spread'} else 0.0

        rows.append({
            'ticker': ticker,
            'signal_close': last_close,
            'spread_type': spread_type,
            'spread_long_strike': long_strike if spread_type else None,
            'spread_short_strike': short_strike if spread_type else None,
            'spread_option_type': ('call' if spread_type == 'bull_call_debit' else 'put') if spread_type else '',
            'spread_ratio_qty': 1 if spread_type else 0,
            'underlying_midpoint': midpoint,
            'intraday_drift_pct': drift * 100 if drift is not None else None,
            'rejection_code': rejection_code,
            'rejection_detail': rejection_detail,
            'bias': bias,
            'signal_score': signal_score,
            'rank_score': rank_score,
            'base_rank_score': base_rank_score,
            'refinement_adjustment': refinement_adjustment,
            'learning_adjustment': learning_adjustment,
            'long_rank_score': long_rank_score,
            'short_rank_score': short_rank_score,
            'setup_quality': setup_quality,
            'thesis_strength': thesis_strength,
            'regime_alignment': regime_alignment,
            'refinement_note': refinement_note,
            'learning_note': learning_note,
            'market_regime': market_regime,
            'expiration_target': expiration or 'unavailable',
            'days_to_expiration': days_to_expiration,
            'options_setup': setup,
            'trade_action': trade_action,
            'expected_move_usd': estimates['expected_move_usd'],
            'estimated_cost_usd': estimates['estimated_cost_usd'],
            'projected_profit_usd': estimates['projected_profit_usd'],
            'projected_return_pct': estimates['projected_return_pct'],
            'max_risk_usd': estimates['max_risk_usd'],
            'allocation_pct': estimates['allocation_pct'],
            'opportunity_source': source,
            'advisor_note': advisor_note,
            'sell_plan': sell_plan,
            'take_profit_pct': take_profit_pct,
            'stop_loss_pct': stop_loss_pct,
            'entry_note': 'Focus on liquid contracts with narrow spreads and avoid chasing weak signals.'
        })

    candidates = pd.DataFrame(rows)
    if not candidates.empty:
        candidates = candidates.sort_values(['rank_score', 'signal_score'], ascending=[False, False])
    return candidates
