from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd


def compute_rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window=window).mean()
    loss = (-delta.clip(upper=0)).rolling(window=window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def annualized_volatility(returns: pd.Series, window: int = 20) -> pd.Series:
    return returns.rolling(window=window).std() * np.sqrt(252)


def choose_structure(score: int, vol: float) -> str:
    if score >= 3:
        return 'Bull call spread' if vol > 0.25 else 'Long call'
    if score <= -3:
        return 'Bear put spread' if vol > 0.25 else 'Long put'
    return 'No trade / wait'


def build_signal_report(market_data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: List[dict] = []

    for ticker, df in market_data.items():
        if df.empty or len(df) < 30:
            rows.append({
                'ticker': ticker,
                'signal_score': 0,
                'bias': 'insufficient_data',
                'suggested_structure': 'No trade',
                'reason': 'Not enough market data for evaluation.',
                'opportunity_source': 'price_action_only',
                'advisor_summary': 'Data is too limited to support a trade thesis.',
                'sell_plan': 'Wait for more data before considering an entry.',
                'bullish_score': 0,
                'bearish_score': 0,
                'setup_quality': 0,
                'long_rank_score': 0.0,
                'short_rank_score': 0.0,
                'thesis_strength': 'insufficient',
            })
            continue

        close = df['Close'].astype(float)
        volume = df['Volume'].astype(float)
        sma_10 = close.rolling(10).mean()
        sma_20 = close.rolling(20).mean()
        rsi = compute_rsi(close)
        returns = close.pct_change()
        vol = annualized_volatility(returns)

        latest_close = float(close.iloc[-1])
        latest_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50.0
        latest_vol = float(vol.iloc[-1]) if pd.notna(vol.iloc[-1]) else 0.0
        latest_volume = float(volume.iloc[-1])
        avg_volume = float(volume.tail(20).mean()) if not volume.tail(20).empty else latest_volume
        one_month_return = (close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else 0.0
        momentum_5d = (close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0.0
        recent_high_20 = float(close.tail(20).max()) if not close.tail(20).empty else latest_close
        recent_low_20 = float(close.tail(20).min()) if not close.tail(20).empty else latest_close
        distance_from_high = (latest_close / recent_high_20 - 1) if recent_high_20 else 0.0
        distance_from_low = (latest_close / recent_low_20 - 1) if recent_low_20 else 0.0
        up_days_ratio = float((returns.tail(10) > 0).mean()) if not returns.tail(10).dropna().empty else 0.5
        down_days_ratio = float((returns.tail(10) < 0).mean()) if not returns.tail(10).dropna().empty else 0.5

        bullish_score = 0
        bearish_score = 0
        bullish_reasons: list[str] = []
        bearish_reasons: list[str] = []
        observations: list[str] = []
        sources: list[str] = []

        if close.iloc[-1] > sma_10.iloc[-1] > sma_20.iloc[-1]:
            bullish_score += 3
            bullish_reasons.append('Trend is above short and medium moving averages')
            sources.append('trend_breakout_scan')
        elif close.iloc[-1] < sma_10.iloc[-1] < sma_20.iloc[-1]:
            bearish_score += 3
            bearish_reasons.append('Trend is below short and medium moving averages')
            sources.append('breakdown_short_scan')

        if momentum_5d > 0.03:
            bullish_score += 2
            bullish_reasons.append('5-day momentum is strong')
            sources.append('momentum_leaders')
        elif momentum_5d < -0.03:
            bearish_score += 2
            bearish_reasons.append('5-day momentum is weak')
            sources.append('weakness_watchlist')

        if latest_rsi > 62:
            bullish_score += 1
            bullish_reasons.append('RSI shows bullish strength')
        elif latest_rsi < 38:
            bearish_score += 1
            bearish_reasons.append('RSI shows bearish weakness')
            sources.append('oversold_breakdown_screen')

        if one_month_return > 0.08:
            bullish_score += 1
            bullish_reasons.append('1-month relative strength is positive')
        elif one_month_return < -0.08:
            bearish_score += 1
            bearish_reasons.append('1-month relative strength is negative')
            sources.append('relative_weakness_shortlist')

        if distance_from_high <= -0.06:
            bearish_score += 1
            bearish_reasons.append('Price remains materially below the 20-day high')
        if distance_from_low >= 0.06:
            bullish_score += 1
            bullish_reasons.append('Price has rebounded cleanly from the 20-day low')

        if down_days_ratio >= 0.6:
            bearish_score += 1
            bearish_reasons.append('Recent session mix favors down days')
        elif up_days_ratio >= 0.6:
            bullish_score += 1
            bullish_reasons.append('Recent session mix favors up days')

        if latest_volume > avg_volume * 1.2:
            observations.append('Volume is elevated, worth closer attention')
            if returns.iloc[-1] < 0:
                bearish_score += 1
                sources.append('high_volume_breakdown')
            elif returns.iloc[-1] > 0:
                bullish_score += 1
                sources.append('high_volume_breakout')

        score = bullish_score - bearish_score
        bias = 'bullish' if score >= 2 else 'bearish' if score <= -2 else 'neutral'
        structure = choose_structure(score, latest_vol)
        setup_quality = max(bullish_score, bearish_score) + (1 if 0.15 <= latest_vol <= 0.60 else 0)
        thesis_strength = 'high' if setup_quality >= 6 else 'medium' if setup_quality >= 4 else 'developing'
        long_rank_score = round(bullish_score * 15 + max(momentum_5d, 0.0) * 100 + max(one_month_return, 0.0) * 120 + (5 if latest_rsi > 55 else 0), 2)
        short_rank_score = round(bearish_score * 15 + max(-momentum_5d, 0.0) * 100 + max(-one_month_return, 0.0) * 120 + (5 if latest_rsi < 45 else 0), 2)

        if structure in {'Bear put spread', 'Long put'}:
            sell_plan = 'Cover part of the trade into sharp downside expansion, or exit if momentum repair invalidates the bearish thesis.'
        elif structure in {'Bull call spread', 'Long call'}:
            sell_plan = 'Consider trimming into strength, at a predefined profit target, or when trend support breaks.'
        else:
            sell_plan = 'No trade until the signal improves and liquidity remains favorable.'

        primary_reasons = bullish_reasons if bias == 'bullish' else bearish_reasons if bias == 'bearish' else bullish_reasons + bearish_reasons
        all_reasons = primary_reasons + observations
        advisor_summary = (
            f"{ticker} is tagged {bias} with a net score of {score}. "
            f"Thesis strength is {thesis_strength}. "
            f"Primary drivers: {'; '.join(all_reasons[:3]) if all_reasons else 'mixed evidence'}."
        )

        rows.append({
            'ticker': ticker,
            'last_close': round(latest_close, 2),
            'signal_score': score,
            'bias': bias,
            'rsi': round(latest_rsi, 1),
            'annualized_volatility': round(latest_vol, 3),
            'suggested_structure': structure,
            'reason': '; '.join(all_reasons) if all_reasons else 'Mixed signals, stay selective.',
            'opportunity_source': ', '.join(sorted(set(sources))) if sources else 'price_action_only',
            'advisor_summary': advisor_summary,
            'sell_plan': sell_plan,
            'bullish_score': bullish_score,
            'bearish_score': bearish_score,
            'setup_quality': int(setup_quality),
            'long_rank_score': long_rank_score,
            'short_rank_score': short_rank_score,
            'thesis_strength': thesis_strength,
        })

    report = pd.DataFrame(rows)
    if not report.empty and 'setup_quality' in report.columns:
        report = report.sort_values(['setup_quality', 'signal_score', 'ticker'], ascending=[False, False, True])
    return report
