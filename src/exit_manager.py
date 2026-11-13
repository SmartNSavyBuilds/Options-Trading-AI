from __future__ import annotations

import json
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


def _paired_option_groups(positions: pd.DataFrame) -> dict[str, tuple[str, str]]:
    groups: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for _, row in positions.iterrows():
        symbol = str(row.get('symbol', '')).strip().upper()
        match = re.fullmatch(r'([A-Z]{1,6})(\d{6})([CP])(\d{8})', symbol)
        if not match:
            continue
        key = (match.group(1), match.group(2), match.group(3))
        groups.setdefault(key, []).append((symbol, str(row.get('side', '')).strip().lower()))

    paired: dict[str, tuple[str, str]] = {}
    for (underlying, expiry, option_type), legs in groups.items():
        sides = {side for _, side in legs}
        if len(legs) < 2 or not {'long', 'short'}.issubset(sides):
            continue
        group_key = f'{underlying}-{expiry}-{option_type}'
        paired_symbols = ','.join(sorted(symbol for symbol, _ in legs))
        for symbol, _ in legs:
            paired[symbol] = (group_key, paired_symbols)
    return paired


def _known_spread_legs(broker_orders: pd.DataFrame | None) -> tuple[dict[str, tuple[str, str]], set[str]]:
    known: dict[str, tuple[str, str]] = {}
    uncertain_underlyings: set[str] = set()
    if broker_orders is None or broker_orders.empty:
        return known, uncertain_underlyings

    active_statuses = {'new', 'accepted', 'partially_filled', 'pending_new', 'filled'}
    for _, order in broker_orders.iterrows():
        if str(order.get('order_class', '')).strip().lower() != 'mleg':
            continue
        status = str(order.get('status', '')).strip().lower()
        reconciliation = str(order.get('spread_reconciliation_status', '')).strip().lower()
        filled = pd.to_numeric(order.get('filled_qty', 0), errors='coerce')
        if status not in active_statuses and (pd.isna(filled) or float(filled) <= 0) and reconciliation not in {'unknown', 'mismatched'}:
            continue
        broker_id = str(order.get('order_id', order.get('client_order_id', 'unknown'))).strip()
        group_key = f'mleg:{broker_id or "unknown"}'
        underlying = str(order.get('underlying_symbol', '')).strip().upper()
        try:
            legs = json.loads(str(order.get('legs_json', '[]') or '[]'))
        except (json.JSONDecodeError, TypeError):
            legs = []
        symbols = sorted({str(leg.get('symbol', '')).strip().upper() for leg in legs if isinstance(leg, dict) and leg.get('symbol')})
        if symbols:
            for symbol in symbols:
                known[symbol] = (group_key, ','.join(symbols))
        elif underlying:
            uncertain_underlyings.add(underlying)
    return known, uncertain_underlyings


def _flagged(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'pending', 'flagged'}


def _assignment_flagged(row: pd.Series) -> bool:
    return any(_flagged(row.get(column, False)) for column in (
        'early_assignment', 'early_assignment_flag', 'early_assignment_risk',
        'assignment_pending', 'assignment_flag', 'exercise_pending',
    ))


def _spread_manual_review_reason(
    paired_symbols: list[str],
    positions: pd.DataFrame,
    broker_orders: pd.DataFrame | None,
) -> str:
    if len(paired_symbols) != 2:
        return 'Spread leg list is incomplete or ambiguous.'

    position_rows = positions[positions['symbol'].astype(str).str.strip().str.upper().isin(paired_symbols)]
    if len(position_rows) != 2 or set(position_rows['symbol'].astype(str).str.strip().str.upper()) != set(paired_symbols):
        return 'A spread leg is missing from the current positions.'
    sides = {
        str(row.get('symbol', '')).strip().upper(): str(row.get('side', '')).strip().lower()
        for _, row in position_rows.iterrows()
    }
    if set(sides.values()) != {'long', 'short'}:
        return 'Spread legs do not have one long and one short position.'
    quantities = pd.to_numeric(position_rows.get('qty', pd.Series(dtype=float)), errors='coerce')
    if quantities.isna().any() or (quantities <= 0).any() or quantities.nunique() != 1:
        return 'Spread leg quantities are mismatched or invalid.'
    if any(_assignment_flagged(row) for _, row in position_rows.iterrows()):
        return 'An early assignment or exercise flag requires manual review.'

    if broker_orders is None or broker_orders.empty:
        return ''
    for _, order in broker_orders.iterrows():
        if str(order.get('order_class', '')).strip().lower() != 'mleg':
            continue
        try:
            legs = json.loads(str(order.get('legs_json', '[]') or '[]'))
        except (json.JSONDecodeError, TypeError):
            legs = []
        order_symbols = {
            str(leg.get('symbol', '')).strip().upper()
            for leg in legs if isinstance(leg, dict) and leg.get('symbol')
        }
        underlying = str(order.get('underlying_symbol', '')).strip().upper()
        if order_symbols and order_symbols != set(paired_symbols):
            continue
        if not order_symbols and underlying not in {symbol.rstrip('0123456789CP') for symbol in paired_symbols}:
            continue
        if any(_assignment_flagged(pd.Series(leg)) for leg in legs if isinstance(leg, dict)):
            return 'A broker-reported early assignment or exercise flag requires manual review.'
        status = str(order.get('status', '')).strip().lower()
        reconciliation = str(order.get('spread_reconciliation_status', '')).strip().lower()
        if status == 'partially_filled' or reconciliation in {'unknown', 'mismatched'}:
            return 'Broker spread fills are partial or mismatched and require reconciliation.'
        if status in {'new', 'accepted', 'pending_new', 'submitted'}:
            return 'A broker spread order is still pending and requires reconciliation.'
        if order_symbols != set(paired_symbols) or reconciliation != 'matched':
            return 'Broker spread legs have not been fully reconciled.'
    return ''


def build_exit_recommendations(
    positions: pd.DataFrame,
    signals: pd.DataFrame,
    broker_orders: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame(columns=['symbol', 'asset_class', 'underlying_symbol', 'side', 'qty', 'unrealized_pl_pct', 'signal_score', 'action', 'reason', 'close_or_exercise_plan', 'decision_window', 'expiration_date', 'days_to_expiration', 'exit_pct', 'priority', 'exit_approved', 'order_status', 'last_submitted_at', 'spread_lifecycle_status', 'spread_group_key', 'paired_symbols', 'spread_alert_required', 'spread_alert_detail'])

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
        paired_groups = _paired_option_groups(positions)
        known_spread_legs, uncertain_underlyings = _known_spread_legs(broker_orders)
        original_actions = result.set_index(result['symbol'].astype(str).str.strip().str.upper())['action'].to_dict()
        result['spread_alert_required'] = False
        result['spread_alert_detail'] = ''
        handled: set[int] = set()
        for index, row in result.iterrows():
            if index in handled:
                continue
            symbol = str(row.get('symbol', '')).strip().upper()
            paired = paired_groups.get(symbol) or known_spread_legs.get(symbol)
            if not paired and str(row.get('asset_class', '')).lower() == 'option' and str(row.get('underlying_symbol', '')).upper() in uncertain_underlyings:
                paired = (f"mleg:{row.get('underlying_symbol', '')}", 'unknown legs')
            result.at[index, 'spread_lifecycle_status'] = 'standalone'
            result.at[index, 'spread_group_key'] = ''
            result.at[index, 'paired_symbols'] = ''
            if paired:
                group_key, paired_symbols = paired
                symbols = [] if paired_symbols.strip().lower() == 'unknown legs' else sorted({
                    item.strip().upper() for item in paired_symbols.split(',') if item.strip()
                })
                member_mask = result['symbol'].astype(str).str.strip().str.upper().isin(symbols)
                member_indices = result.index[member_mask].tolist()
                if not member_indices:
                    member_indices = [index]
                reason = _spread_manual_review_reason(symbols, positions, broker_orders)
                if reason:
                    for member_index in member_indices:
                        result.at[member_index, 'action'] = 'hold'
                        result.at[member_index, 'reason'] = f'MANUAL_REVIEW_REQUIRED: {reason}'
                        result.at[member_index, 'close_or_exercise_plan'] = 'Manual review required. Do not close this leg independently.'
                        result.at[member_index, 'decision_window'] = 'Manual review required before any order.'
                        result.at[member_index, 'exit_pct'] = 0.0
                        result.at[member_index, 'priority'] = ACTION_PRIORITY['hold']
                        result.at[member_index, 'exit_approved'] = False
                        result.at[member_index, 'spread_lifecycle_status'] = 'MANUAL_REVIEW_REQUIRED'
                        result.at[member_index, 'spread_alert_required'] = True
                        result.at[member_index, 'spread_alert_detail'] = reason
                        result.at[member_index, 'spread_group_key'] = group_key
                        result.at[member_index, 'paired_symbols'] = ','.join(symbols) if symbols else paired_symbols
                        handled.add(member_index)
                    continue

                group_actions = [original_actions.get(item, 'hold') for item in symbols]
                spread_action = min(group_actions, key=lambda action: ACTION_PRIORITY.get(action, 9))
                for member_index in member_indices:
                    result.at[member_index, 'action'] = spread_action
                    result.at[member_index, 'reason'] = (
                        'Close the healthy matched vertical as one atomic paired order.'
                        if spread_action != 'hold' else 'Matched vertical remains within its current exit thresholds.'
                    )
                    result.at[member_index, 'close_or_exercise_plan'] = 'Close both matched option legs together with one MLeg order.'
                    result.at[member_index, 'decision_window'] = 'Review the paired close at the next execution cycle.'
                    result.at[member_index, 'exit_pct'] = ACTION_EXIT_PCT.get(spread_action, 0.0)
                    result.at[member_index, 'priority'] = ACTION_PRIORITY.get(spread_action, 9)
                    result.at[member_index, 'spread_lifecycle_status'] = 'healthy_matched'
                    result.at[member_index, 'spread_group_key'] = group_key
                    result.at[member_index, 'paired_symbols'] = ','.join(symbols)
                    handled.add(member_index)
        result = result.sort_values(['priority', 'symbol'], ascending=[True, True])
    return result


def save_exit_recommendations(project_dir: Path, recommendations: pd.DataFrame) -> Path:
    output_path = project_dir / 'outputs' / 'exit_recommendations.csv'
    output_path.parent.mkdir(exist_ok=True)
    recommendations.to_csv(output_path, index=False)
    return output_path
