from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


ACTION_EXIT_PCT = {
    'hold': 0.0,
    'trim_winner': 50.0,
    'reduce_or_close': 100.0,
    'stop_out': 100.0,
    'cover_or_reduce': 100.0,
}

ACTION_PRIORITY = {
    'stop_out': 1,
    'cover_or_reduce': 2,
    'reduce_or_close': 3,
    'trim_winner': 4,
    'hold': 9,
}


def _parse_option_metadata(symbol: str) -> tuple[str, str | None, int | None]:
    text = str(symbol or '').upper()
    match = re.match(r'^([A-Z]+)(\d{6})([CP])\d+', text)
    if not match:
        return text, None, None

    underlying = match.group(1)
    raw_date = match.group(2)
    expiry = pd.to_datetime(raw_date, format='%y%m%d', errors='coerce')
    days = None
    if pd.notna(expiry):
        days = int((expiry.normalize() - pd.Timestamp.now().normalize()).days)
        return underlying, expiry.strftime('%m-%d-%Y'), days
    return underlying, None, None


def build_exit_recommendations(positions: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame(columns=['symbol', 'asset_class', 'underlying_symbol', 'side', 'qty', 'unrealized_pl_pct', 'signal_score', 'action', 'reason', 'close_or_exercise_plan', 'decision_window', 'expiration_date', 'days_to_expiration', 'exit_pct', 'priority', 'exit_approved', 'order_status', 'last_submitted_at'])

    signal_map = signals.rename(columns={'ticker': 'symbol'}).copy() if not signals.empty else pd.DataFrame(columns=['symbol'])
    signal_lookup = signal_map.set_index('symbol')[['signal_score', 'bias']].to_dict('index') if not signal_map.empty else {}

    rows: list[dict[str, object]] = []
    for _, row in positions.iterrows():
        symbol = str(row.get('symbol', ''))
        underlying_symbol, expiration_date, days_to_expiration = _parse_option_metadata(symbol)
        signal_info = signal_lookup.get(underlying_symbol, signal_lookup.get(symbol, {}))

        market_value = float(row.get('market_value', 0.0) or 0.0)
        unrealized_pl = float(row.get('unrealized_pl', 0.0) or 0.0)
        raw_signal_score = signal_info.get('signal_score', 0)
        signal_score = int(0 if pd.isna(raw_signal_score) else raw_signal_score)
        bias = str(signal_info.get('bias', 'neutral')).lower()
        side = str(row.get('side', '')).lower()
        unrealized_pl_pct = (unrealized_pl / market_value * 100) if market_value else 0.0
        asset_class = 'option' if expiration_date else 'stock'

        action = 'hold'
        reason = 'Signal remains intact and no exit threshold has been crossed.'
        close_or_exercise_plan = 'Continue holding while the signal remains intact and risk stays controlled.'
        decision_window = 'Review at the next market check.'

        if side == 'long' and unrealized_pl_pct <= -2.0:
            action = 'stop_out'
            reason = 'Paper loss breached the initial stop-loss threshold.'
        elif side == 'long' and signal_score <= 0:
            action = 'reduce_or_close'
            reason = 'The long thesis has weakened or turned neutral/bearish.'
        elif side == 'long' and unrealized_pl_pct >= 4.0:
            action = 'trim_winner'
            reason = 'Paper gain exceeded the initial take-profit threshold.'
        elif side == 'short' and unrealized_pl_pct <= -2.0:
            action = 'stop_out'
            reason = 'The short position is moving against the thesis and should be closed.'
        elif side == 'short' and bias == 'bullish':
            action = 'cover_or_reduce'
            reason = 'The short thesis has weakened because the model bias is bullish.'
        elif side == 'short' and unrealized_pl_pct >= 4.0:
            action = 'trim_winner'
            reason = 'The short position has reached an early take-profit zone.'

        if asset_class == 'option':
            if days_to_expiration is not None and days_to_expiration <= 1:
                close_or_exercise_plan = 'Final expiry review: close before the bell if the contract is out-of-the-money; only consider exercise if it is in-the-money and buying power supports it.'
                decision_window = 'Urgent: same day or next session.'
            elif days_to_expiration is not None and days_to_expiration <= 5:
                close_or_exercise_plan = 'Tighten the stop and review daily; close early if momentum fades, otherwise hold into the catalyst window.'
                decision_window = f'Review daily into expiry week ({days_to_expiration} days left).'
            else:
                close_or_exercise_plan = 'Manage as a premium position: trim strength, close on signal deterioration, and avoid passive expiry risk.'
                decision_window = 'Review every trading day.'
        else:
            if action == 'trim_winner':
                close_or_exercise_plan = 'Trim part of the stock position into strength and keep the rest only if the signal stays favorable.'
            elif action in {'reduce_or_close', 'stop_out', 'cover_or_reduce'}:
                close_or_exercise_plan = 'Close or reduce the stock position on the next execution cycle to protect capital.'

        rows.append({
            'symbol': symbol,
            'asset_class': asset_class,
            'underlying_symbol': underlying_symbol,
            'side': side,
            'qty': float(row.get('qty', 0.0) or 0.0),
            'unrealized_pl_pct': round(unrealized_pl_pct, 2),
            'signal_score': signal_score,
            'action': action,
            'reason': reason,
            'close_or_exercise_plan': close_or_exercise_plan,
            'decision_window': decision_window,
            'expiration_date': expiration_date or '',
            'days_to_expiration': days_to_expiration if days_to_expiration is not None else '',
            'exit_pct': ACTION_EXIT_PCT.get(action, 0.0),
            'priority': ACTION_PRIORITY.get(action, 9),
            'exit_approved': False,
            'order_status': 'monitor',
            'last_submitted_at': '',
        })

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values(['priority', 'symbol'], ascending=[True, True])
    return result


def save_exit_recommendations(project_dir: Path, recommendations: pd.DataFrame) -> Path:
    output_path = project_dir / 'outputs' / 'exit_recommendations.csv'
    output_path.parent.mkdir(exist_ok=True)
    recommendations.to_csv(output_path, index=False)
    return output_path
