from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ACTIVE_EXECUTION_STATUSES = {'new', 'accepted', 'filled', 'partially_filled', 'submitted', 'pending_new'}
JOURNAL_COLUMNS = [
    'journaled_at_utc',
    'symbol',
    'underlying_symbol',
    'asset_class',
    'side',
    'qty',
    'market_value',
    'unrealized_pl',
    'journal_status',
    'last_execution_status',
    'last_execution_time',
    'execution_style',
    'note',
]
ALERT_COLUMNS = [
    'alerted_at_utc',
    'symbol',
    'underlying_symbol',
    'alert_type',
    'severity',
    'escalation_action',
    'action',
    'reason',
    'portfolio_impact_pct',
    'days_to_expiration',
]
STRATEGY_ATTRIBUTION_COLUMNS = [
    'strategy_bucket',
    'positions',
    'winning_positions',
    'losing_positions',
    'win_rate',
    'total_market_value',
    'total_unrealized_pl',
    'avg_unrealized_pl',
]
EXECUTION_QUALITY_COLUMNS = [
    'execution_style',
    'submitted_orders',
    'filled_orders',
    'working_orders',
    'rejected_orders',
    'fill_rate_pct',
    'rejection_rate_pct',
]
LEARNING_FEEDBACK_COLUMNS = [
    'ticker',
    'positions',
    'win_rate',
    'avg_unrealized_pl',
    'fill_rate_pct',
    'learning_adjustment',
    'learning_note',
]
OPEN_TRADE_TIMELINE_COLUMNS = [
    'symbol',
    'asset_class',
    'qty',
    'market_value',
    'unrealized_pl',
    'action',
    'expected_close_date',
    'decision_window',
    'close_or_exercise_plan',
    'reason',
]


def extract_underlying_symbol(symbol: str) -> str:
    match = re.match(r'([A-Z]+)', str(symbol or '').upper())
    return match.group(1) if match else ''


def _is_option_symbol(symbol: str) -> bool:
    return bool(re.search(r'\d{6}[CP]', str(symbol or '').upper()))


def _execution_display_tickers(execution_log: pd.DataFrame) -> pd.Series:
    if execution_log.empty:
        return pd.Series(dtype=str)

    base = execution_log.get('underlying_ticker', pd.Series('', index=execution_log.index)).fillna('').astype(str).str.upper()
    fallback = execution_log.get('ticker', pd.Series('', index=execution_log.index)).fillna('').astype(str).str.upper()
    return base.where(base.ne(''), fallback)


def humanize_display_text(value: object) -> object:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ''

    text = str(value).strip()
    if not text or text.lower() in {'nan', 'none'}:
        return ''
    if '://' in text:
        return text

    normalized = text.replace('_', ' ')
    normalized = re.sub(r'(?<=[A-Za-z])-(?=[A-Za-z])', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    if normalized == text:
        return text
    if normalized.replace(' ', '').isalnum() and normalized.upper() == normalized and any(ch.isalpha() for ch in normalized):
        return normalized

    words = []
    for word in normalized.split(' '):
        words.append(word if any(char.isdigit() for char in word) else word.capitalize())
    return ' '.join(words)


def _estimate_expected_close_date(action: object, expiration_date: object, reference_date: str | None = None) -> str:
    base_date = pd.Timestamp(reference_date).normalize() if reference_date else pd.Timestamp.now().normalize()
    expiry = pd.to_datetime(expiration_date, errors='coerce')
    if pd.notna(expiry):
        return expiry.strftime('%m-%d-%Y')

    action_key = str(action or '').strip().lower()
    if action_key in {'stop_out', 'reduce_or_close', 'cover_or_reduce'}:
        return (base_date + pd.offsets.BDay(1)).strftime('%m-%d-%Y')
    if action_key == 'trim_winner':
        return (base_date + pd.offsets.BDay(2)).strftime('%m-%d-%Y')
    return (base_date + pd.offsets.BDay(5)).strftime('%m-%d-%Y')


def build_open_trade_timeline(journal: pd.DataFrame, exits: pd.DataFrame, reference_date: str | None = None) -> pd.DataFrame:
    if journal.empty:
        return pd.DataFrame(columns=OPEN_TRADE_TIMELINE_COLUMNS)

    exit_view = exits.copy() if not exits.empty else pd.DataFrame(columns=['symbol'])
    merged = journal.merge(exit_view, on='symbol', how='left', suffixes=('', '_exit'))

    for col, default in {
        'asset_class': 'stock',
        'qty': 0.0,
        'market_value': 0.0,
        'unrealized_pl': 0.0,
        'action': 'hold',
        'decision_window': 'Review at the next market check.',
        'close_or_exercise_plan': 'Continue holding while the signal remains intact and risk stays controlled.',
        'reason': 'No close reasoning available.',
        'expiration_date': '',
    }.items():
        if col not in merged.columns:
            merged[col] = default
        merged[col] = merged[col].fillna(default)

    merged['expected_close_date'] = merged.apply(
        lambda row: _estimate_expected_close_date(row.get('action', 'hold'), row.get('expiration_date', ''), reference_date=reference_date),
        axis=1,
    )
    merged['asset_class'] = merged['asset_class'].map(humanize_display_text)
    merged['action'] = merged['action'].map(humanize_display_text)

    return merged[OPEN_TRADE_TIMELINE_COLUMNS].sort_values(['expected_close_date', 'symbol']).reset_index(drop=True)


def build_open_executed_underlyings(positions: pd.DataFrame, execution_log: pd.DataFrame) -> set[str]:
    underlyings: set[str] = set()

    if not positions.empty and 'symbol' in positions.columns:
        symbols = positions['symbol'].astype(str).map(extract_underlying_symbol)
        underlyings.update({symbol for symbol in symbols if symbol})

    if not execution_log.empty:
        working = execution_log.copy()
        statuses = working.get('status', pd.Series('', index=working.index)).astype(str).str.lower()
        active = working.loc[statuses.isin(ACTIVE_EXECUTION_STATUSES)].copy()
        if not active.empty:
            names = _execution_display_tickers(active)
            underlyings.update({symbol for symbol in names if symbol})

    return underlyings


def add_execution_status_to_candidates(candidates: pd.DataFrame, positions: pd.DataFrame, execution_log: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()

    marked = candidates.copy()
    open_executed = build_open_executed_underlyings(positions, execution_log)
    marked['is_open_executed'] = marked['ticker'].astype(str).str.upper().isin(open_executed)
    marked['execution_state'] = marked['is_open_executed'].map({True: 'Open / Executed', False: 'Research Queue'})
    marked['execution_badge'] = marked['is_open_executed'].map({True: '🟢 Open / Executed', False: '⚪ Research Queue'})
    marked['display_color_label'] = marked.apply(
        lambda row: 'Open / Executed' if bool(row.get('is_open_executed', False)) else str(row.get('bias', 'neutral')).lower(),
        axis=1,
    )
    return marked


def build_performance_journal(positions: pd.DataFrame, execution_log: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame(columns=JOURNAL_COLUMNS)

    journaled_at = datetime.now(timezone.utc).isoformat()
    log = execution_log.copy() if not execution_log.empty else pd.DataFrame()

    if not log.empty:
        log['display_ticker'] = _execution_display_tickers(log)
        log = log.sort_values('submitted_at_utc') if 'submitted_at_utc' in log.columns else log

    rows: list[dict] = []
    for _, position in positions.iterrows():
        symbol = str(position.get('symbol', '')).upper()
        underlying = extract_underlying_symbol(symbol)
        asset_class = 'option' if _is_option_symbol(symbol) else 'stock'
        unrealized_pl = float(position.get('unrealized_pl', 0.0) or 0.0)

        if unrealized_pl > 0:
            journal_status = 'open_winner'
            note = 'Open position is currently profitable.'
        elif unrealized_pl < 0:
            journal_status = 'open_loser'
            note = 'Open position is currently below entry and should be monitored.'
        else:
            journal_status = 'open_flat'
            note = 'Open position is roughly flat.'

        last_status = ''
        last_time = ''
        execution_style = ''
        if not log.empty:
            if asset_class == 'option':
                related = log[log.get('ticker', pd.Series('', index=log.index)).astype(str).str.upper().eq(symbol)]
                if related.empty and underlying:
                    related = log[
                        log['display_ticker'].eq(underlying)
                        & log.get('execution_style', pd.Series('', index=log.index)).astype(str).str.contains('option', case=False, na=False)
                    ]
            else:
                related = log[
                    log['display_ticker'].eq(symbol)
                    & ~log.get('execution_style', pd.Series('', index=log.index)).astype(str).str.contains('option', case=False, na=False)
                ]
            if not related.empty:
                latest = related.iloc[-1]
                last_status = str(latest.get('status', ''))
                last_time = str(latest.get('submitted_at_utc', ''))
                execution_style = str(latest.get('execution_style', ''))

        rows.append(
            {
                'journaled_at_utc': journaled_at,
                'symbol': symbol,
                'underlying_symbol': underlying,
                'asset_class': asset_class,
                'side': str(position.get('side', 'unknown')),
                'qty': float(position.get('qty', 0.0) or 0.0),
                'market_value': float(position.get('market_value', 0.0) or 0.0),
                'unrealized_pl': unrealized_pl,
                'journal_status': journal_status,
                'last_execution_status': last_status,
                'last_execution_time': last_time,
                'execution_style': execution_style,
                'note': note,
            }
        )

    journal = pd.DataFrame(rows, columns=JOURNAL_COLUMNS)
    return journal.sort_values(['asset_class', 'underlying_symbol', 'symbol']).reset_index(drop=True)


def build_priority_alerts(journal: pd.DataFrame, exits: pd.DataFrame) -> pd.DataFrame:
    if journal.empty:
        return pd.DataFrame(columns=ALERT_COLUMNS)

    exits_working = exits.copy() if not exits.empty else pd.DataFrame(columns=['symbol', 'action', 'reason', 'days_to_expiration', 'priority'])
    alerts: list[dict] = []
    alerted_at = datetime.now(timezone.utc).isoformat()

    for _, row in journal.iterrows():
        symbol = str(row.get('symbol', ''))
        exit_row = exits_working.loc[exits_working.get('symbol', pd.Series(dtype=str)).astype(str) == symbol]
        action = 'hold'
        reason = str(row.get('note', ''))
        days_to_expiration = ''
        priority = 9
        if not exit_row.empty:
            match = exit_row.iloc[0]
            action = str(match.get('action', 'hold'))
            reason = str(match.get('reason', reason))
            days_to_expiration = match.get('days_to_expiration', '')
            priority = int(match.get('priority', 9) or 9)

        market_value = float(row.get('market_value', 0.0) or 0.0)
        portfolio_impact_pct = round(abs(float(row.get('unrealized_pl', 0.0) or 0.0)) / max(market_value, 1.0) * 100, 2)
        alert_type = ''
        severity = ''
        escalation_action = ''
        if action in {'stop_out', 'reduce_or_close', 'cover_or_reduce'} or priority <= 2:
            alert_type = 'urgent_risk'
            severity = 'high'
            escalation_action = 'page_operator_now'
        elif str(row.get('asset_class', '')).lower() == 'option' and str(days_to_expiration).strip() not in {'', 'nan'}:
            try:
                if int(float(days_to_expiration)) <= 3:
                    alert_type = 'expiry_watch'
                    severity = 'high'
                    escalation_action = 'review_next_session'
            except ValueError:
                pass
        elif portfolio_impact_pct >= 8:
            alert_type = 'drawdown_watch'
            severity = 'medium'
            escalation_action = 'tighten_risk_limits'
        elif str(row.get('journal_status', '')).lower() == 'open_winner':
            alert_type = 'profit_watch'
            severity = 'medium'
            escalation_action = 'trail_and_review'

        if alert_type:
            alerts.append(
                {
                    'alerted_at_utc': alerted_at,
                    'symbol': symbol,
                    'underlying_symbol': str(row.get('underlying_symbol', '')),
                    'alert_type': alert_type,
                    'severity': severity,
                    'escalation_action': escalation_action,
                    'action': action,
                    'reason': reason,
                    'portfolio_impact_pct': portfolio_impact_pct,
                    'days_to_expiration': days_to_expiration,
                }
            )

    if not alerts:
        return pd.DataFrame(columns=ALERT_COLUMNS)
    return pd.DataFrame(alerts, columns=ALERT_COLUMNS).sort_values(['severity', 'symbol'], ascending=[True, True]).reset_index(drop=True)


def build_performance_summary(journal: pd.DataFrame) -> pd.DataFrame:
    if journal.empty:
        return pd.DataFrame(
            [
                {
                    'journaled_at_utc': datetime.now(timezone.utc).isoformat(),
                    'open_positions': 0,
                    'stock_positions': 0,
                    'option_positions': 0,
                    'winning_positions': 0,
                    'losing_positions': 0,
                    'total_market_value': 0.0,
                    'total_unrealized_pl': 0.0,
                }
            ]
        )

    return pd.DataFrame(
        [
            {
                'journaled_at_utc': journal['journaled_at_utc'].iloc[0],
                'open_positions': int(len(journal)),
                'stock_positions': int(journal['asset_class'].astype(str).eq('stock').sum()),
                'option_positions': int(journal['asset_class'].astype(str).eq('option').sum()),
                'winning_positions': int(journal['journal_status'].astype(str).eq('open_winner').sum()),
                'losing_positions': int(journal['journal_status'].astype(str).eq('open_loser').sum()),
                'total_market_value': float(journal['market_value'].astype(float).sum()),
                'total_unrealized_pl': float(journal['unrealized_pl'].astype(float).sum()),
            }
        ]
    )


def build_strategy_attribution(journal: pd.DataFrame) -> pd.DataFrame:
    if journal.empty:
        return pd.DataFrame(columns=STRATEGY_ATTRIBUTION_COLUMNS)

    working = journal.copy()
    working['strategy_bucket'] = working.get('execution_style', pd.Series('', index=working.index)).replace('', 'unclassified')
    working['market_value'] = pd.to_numeric(working.get('market_value', 0.0), errors='coerce').fillna(0.0)
    working['unrealized_pl'] = pd.to_numeric(working.get('unrealized_pl', 0.0), errors='coerce').fillna(0.0)
    grouped = working.groupby('strategy_bucket', dropna=False).agg(
        positions=('strategy_bucket', 'count'),
        winning_positions=('journal_status', lambda s: int(s.astype(str).eq('open_winner').sum())),
        losing_positions=('journal_status', lambda s: int(s.astype(str).eq('open_loser').sum())),
        total_market_value=('market_value', 'sum'),
        total_unrealized_pl=('unrealized_pl', 'sum'),
        avg_unrealized_pl=('unrealized_pl', 'mean'),
    ).reset_index()
    grouped['win_rate'] = (grouped['winning_positions'] / grouped['positions'].clip(lower=1)).round(2)
    grouped['strategy_bucket'] = grouped['strategy_bucket'].map(humanize_display_text)
    return grouped[STRATEGY_ATTRIBUTION_COLUMNS].sort_values(['total_unrealized_pl', 'strategy_bucket'], ascending=[False, True]).reset_index(drop=True)


def build_execution_quality_report(execution_log: pd.DataFrame) -> pd.DataFrame:
    if execution_log.empty:
        return pd.DataFrame(columns=EXECUTION_QUALITY_COLUMNS)

    working = execution_log.copy()
    working['execution_style'] = working.get('execution_style', pd.Series('', index=working.index)).replace('', 'unclassified')
    working['status'] = working.get('status', pd.Series('', index=working.index)).astype(str).str.lower()

    grouped = working.groupby('execution_style', dropna=False).agg(
        submitted_orders=('status', 'count'),
        filled_orders=('status', lambda s: int(s.eq('filled').sum())),
        working_orders=('status', lambda s: int(s.isin({'accepted', 'pending_new', 'new', 'submitted', 'partially_filled'}).sum())),
        rejected_orders=('status', lambda s: int(s.isin({'rejected', 'canceled', 'cancelled'}).sum())),
    ).reset_index()
    grouped['fill_rate_pct'] = (grouped['filled_orders'] / grouped['submitted_orders'].clip(lower=1) * 100).round(2)
    grouped['rejection_rate_pct'] = (grouped['rejected_orders'] / grouped['submitted_orders'].clip(lower=1) * 100).round(2)
    grouped['execution_style'] = grouped['execution_style'].map(humanize_display_text)
    return grouped[EXECUTION_QUALITY_COLUMNS].sort_values(['fill_rate_pct', 'execution_style'], ascending=[False, True]).reset_index(drop=True)


def build_learning_feedback(journal: pd.DataFrame, execution_log: pd.DataFrame) -> pd.DataFrame:
    quality = build_execution_quality_report(execution_log)
    portfolio_fill_rate = float(quality['fill_rate_pct'].mean()) if not quality.empty and 'fill_rate_pct' in quality.columns else 0.0

    if journal.empty:
        note = 'No open paper positions yet, so the learning loop is still warming up.'
        adjustment = -0.5 if portfolio_fill_rate and portfolio_fill_rate < 40 else 0.0
        return pd.DataFrame(
            [{'ticker': '__PORTFOLIO__', 'positions': 0, 'win_rate': 0.0, 'avg_unrealized_pl': 0.0, 'fill_rate_pct': portfolio_fill_rate, 'learning_adjustment': adjustment, 'learning_note': note}],
            columns=LEARNING_FEEDBACK_COLUMNS,
        )

    working = journal.copy()
    working['ticker'] = working.get('underlying_symbol', pd.Series('', index=working.index)).replace('', pd.NA)
    working['ticker'] = working['ticker'].fillna(working.get('symbol', pd.Series('', index=working.index))).astype(str).str.upper()
    working['unrealized_pl'] = pd.to_numeric(working.get('unrealized_pl', 0.0), errors='coerce').fillna(0.0)
    working['is_winner'] = working.get('journal_status', pd.Series('', index=working.index)).astype(str).eq('open_winner')

    grouped = working.groupby('ticker', dropna=False).agg(
        positions=('ticker', 'count'),
        winning_positions=('is_winner', 'sum'),
        avg_unrealized_pl=('unrealized_pl', 'mean'),
    ).reset_index()
    grouped['win_rate'] = (grouped['winning_positions'] / grouped['positions'].clip(lower=1)).round(2)
    grouped['fill_rate_pct'] = portfolio_fill_rate

    def _adjust(row: pd.Series) -> pd.Series:
        adjustment = 0.0
        notes: list[str] = []
        win_rate = float(row.get('win_rate', 0.0) or 0.0)
        avg_pl = float(row.get('avg_unrealized_pl', 0.0) or 0.0)
        fill_rate = float(row.get('fill_rate_pct', 0.0) or 0.0)

        if win_rate >= 0.6 and avg_pl > 0:
            adjustment += 3.0
            notes.append('Recent paper results in this name have been constructive.')
        elif win_rate <= 0.4 and avg_pl < 0:
            adjustment -= 3.0
            notes.append('Recent paper results in this name have been weak.')
        else:
            notes.append('Recent paper results are still mixed for this name.')

        if fill_rate >= 70:
            adjustment += 1.0
            notes.append('Execution quality has been supportive.')
        elif 0 < fill_rate < 40:
            adjustment -= 1.0
            notes.append('Execution quality has been soft, so rankings stay conservative.')

        return pd.Series({'learning_adjustment': round(adjustment, 2), 'learning_note': ' '.join(notes)})

    grouped[['learning_adjustment', 'learning_note']] = grouped.apply(_adjust, axis=1)
    grouped = grouped[['ticker', 'positions', 'win_rate', 'avg_unrealized_pl', 'fill_rate_pct', 'learning_adjustment', 'learning_note']]

    portfolio_positions = int(len(working))
    portfolio_win_rate = round(float(working['is_winner'].mean()), 2) if portfolio_positions else 0.0
    portfolio_avg_pl = round(float(working['unrealized_pl'].mean()), 2) if portfolio_positions else 0.0
    portfolio_adjustment = 0.0
    portfolio_notes: list[str] = []
    if portfolio_win_rate >= 0.55 and portfolio_avg_pl > 0:
        portfolio_adjustment += 1.5
        portfolio_notes.append('Recent paper trades are broadly supporting the current playbook.')
    elif portfolio_win_rate <= 0.45 and portfolio_avg_pl < 0:
        portfolio_adjustment -= 1.5
        portfolio_notes.append('Recent paper trades have been soft, so new ideas are ranked more cautiously.')
    else:
        portfolio_notes.append('The broader paper book is still mixed, so the system stays selective.')

    if portfolio_fill_rate >= 70:
        portfolio_adjustment += 0.5
    elif 0 < portfolio_fill_rate < 40:
        portfolio_adjustment -= 0.5

    portfolio_row = pd.DataFrame(
        [{'ticker': '__PORTFOLIO__', 'positions': portfolio_positions, 'win_rate': portfolio_win_rate, 'avg_unrealized_pl': portfolio_avg_pl, 'fill_rate_pct': portfolio_fill_rate, 'learning_adjustment': round(portfolio_adjustment, 2), 'learning_note': ' '.join(portfolio_notes)}],
        columns=LEARNING_FEEDBACK_COLUMNS,
    )

    return pd.concat([portfolio_row, grouped], ignore_index=True)


def save_performance_outputs(project_dir: Path, journal: pd.DataFrame, exits: pd.DataFrame | None = None, execution_log: pd.DataFrame | None = None) -> tuple[Path, Path, Path, Path, Path, Path]:
    output_dir = project_dir / 'outputs'
    output_dir.mkdir(exist_ok=True)

    journal_path = output_dir / 'performance_journal.csv'
    summary_path = output_dir / 'performance_summary.csv'
    alerts_path = output_dir / 'alerts_feed.csv'
    attribution_path = output_dir / 'strategy_attribution.csv'
    quality_path = output_dir / 'execution_quality.csv'
    learning_path = output_dir / 'learning_feedback.csv'

    summary = build_performance_summary(journal)
    alerts = build_priority_alerts(journal, exits if exits is not None else pd.DataFrame())
    attribution = build_strategy_attribution(journal)
    quality = build_execution_quality_report(execution_log if execution_log is not None else pd.DataFrame())
    learning = build_learning_feedback(journal, execution_log if execution_log is not None else pd.DataFrame())
    journal.to_csv(journal_path, index=False)
    summary.to_csv(summary_path, index=False)
    alerts.to_csv(alerts_path, index=False)
    attribution.to_csv(attribution_path, index=False)
    quality.to_csv(quality_path, index=False)
    learning.to_csv(learning_path, index=False)
    return journal_path, summary_path, alerts_path, attribution_path, quality_path, learning_path
