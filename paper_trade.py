from __future__ import annotations

from bootstrap_path import ensure_project_root_on_path

ensure_project_root_on_path()

import os
import re
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path

import pandas as pd

from src.execution import _load_env_file
from src.iv_history import safety_database_path
from src.risk_guardrails import apply_exposure_guardrails, build_exposure_summary, build_risk_overview, build_stress_test_table, save_exposure_summary, save_risk_outputs


OUTPUT_DIR = Path(__file__).resolve().parent / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)
WIKI_DIR = Path(__file__).resolve().parent / 'wiki'
DISCOVERY_COOLDOWN_REASONS = {'STALE_QUOTE', 'WIDE_SPREAD', 'NO_MATCHING_SPREAD'}


def _extract_underlying_ticker(symbol: str) -> str:
    match = re.match(r'([A-Z]+)', str(symbol or '').upper())
    return match.group(1) if match else ''


def _is_option_symbol(symbol: str) -> bool:
    """Return True if the symbol looks like an options contract (e.g. AAPL260117C00150000)."""
    return bool(re.search(r'\d{6}[CP]\d+', str(symbol or '').upper()))


def load_existing_tickers() -> set[str]:
    """Return tickers that already have an active OPTIONS position or an active queue entry.
    Plain stock/ETF holdings are NOT excluded — they are a separate strategy from options spreads.
    """
    existing: set[str] = set()

    # Only block on existing OPTION positions, not stock positions
    positions_file = OUTPUT_DIR / 'broker_positions.csv'
    if positions_file.exists():
        positions = pd.read_csv(positions_file)
        if not positions.empty and 'symbol' in positions.columns:
            option_positions = positions[positions['symbol'].astype(str).apply(_is_option_symbol)]
            existing.update(filter(None, (_extract_underlying_ticker(symbol) for symbol in option_positions['symbol'].astype(str))))

    # Block on active entries already in the paper trade queue
    queue_file = OUTPUT_DIR / 'paper_trade_queue.csv'
    if queue_file.exists():
        previous = pd.read_csv(queue_file)
        if not previous.empty and 'ticker' in previous.columns:
            active_statuses = {'new', 'accepted', 'partially_filled', 'submitted', 'pending_new'}
            stock_mask = previous.get('order_status', pd.Series(dtype=str)).astype(str).str.lower().isin(active_statuses) if 'order_status' in previous.columns else pd.Series(False, index=previous.index)
            option_mask = previous.get('option_order_status', pd.Series(dtype=str)).astype(str).str.lower().isin(active_statuses) if 'option_order_status' in previous.columns else pd.Series(False, index=previous.index)
            existing.update(previous.loc[stock_mask | option_mask, 'ticker'].astype(str).str.upper().tolist())

    return existing


def load_positions_snapshot() -> pd.DataFrame:
    positions_file = OUTPUT_DIR / 'broker_positions.csv'
    if not positions_file.exists():
        return pd.DataFrame()
    return pd.read_csv(positions_file)


def load_candidates() -> pd.DataFrame:
    candidates = sorted(OUTPUT_DIR.glob('options_candidates*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return pd.DataFrame()

    df = pd.read_csv(candidates[0])
    defaults = {
        'rank_score': 0.0,
        'projected_return_pct': 0.0,
        'max_risk_usd': 0.0,
        'allocation_pct': 0.0,
        'options_setup': 'No trade',
        'bias': 'neutral',
        'signal_score': 0,
        'signal_close': 0.0,
        'underlying_midpoint': 0.0,
        'intraday_drift_pct': 0.0,
        'rejection_code': '',
        'rejection_detail': '',
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
    discovery_path = OUTPUT_DIR / 'opportunity_discovery.csv'
    if discovery_path.exists():
        try:
            discovery = pd.read_csv(discovery_path)
            columns = ['ticker', 'catalyst_checked_at_utc', 'catalyst_status',
                       'catalyst_reason', 'earnings_dates', 'headline_count_60m']
            if all(column in discovery.columns for column in columns):
                df = df.drop(columns=[column for column in columns[1:] if column in df.columns])
                df = df.merge(discovery[columns].drop_duplicates('ticker', keep='last'), on='ticker', how='left')
        except (OSError, ValueError, pd.errors.ParserError):
            pass
    return df


def _discovery_cooldown_minutes() -> int:
    raw = str(os.getenv('DISCOVERY_COOLDOWN_MINUTES', '20') or '20').strip()
    try:
        return max(0, min(int(raw), 24 * 60))
    except ValueError:
        return 20


def _discovery_top_k(default: int = 3) -> int:
    raw = str(os.getenv('DISCOVERY_TOP_K', str(default)) or str(default)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _discovery_db_path() -> Path:
    return safety_database_path(OUTPUT_DIR.parent)


def _ensure_discovery_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        'CREATE TABLE IF NOT EXISTS discovery_skip_log ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'logged_at_utc TEXT NOT NULL, '
        'ticker TEXT NOT NULL, '
        'reason TEXT NOT NULL, '
        'detail TEXT NOT NULL, '
        'source_rejection_code TEXT NOT NULL, '
        'cooldown_until_utc TEXT NOT NULL)'
    )
    connection.execute(
        'CREATE TABLE IF NOT EXISTS discovery_symbol_cooldowns ('
        'ticker TEXT PRIMARY KEY, '
        'reason TEXT NOT NULL, '
        'detail TEXT NOT NULL, '
        'cooldown_until_utc TEXT NOT NULL, '
        'updated_at_utc TEXT NOT NULL)'
    )


def _load_active_cooldown_until(
    connection: sqlite3.Connection,
    ticker: str,
    now_utc: datetime,
) -> datetime | None:
    row = connection.execute(
        'SELECT cooldown_until_utc FROM discovery_symbol_cooldowns WHERE ticker = ?',
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    text = str(row[0] or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed if parsed > now_utc else None


def _record_discovery_skip(
    connection: sqlite3.Connection,
    ticker: str,
    reason: str,
    detail: str,
    source_rejection_code: str,
    cooldown_until: datetime | None,
    logged_at_utc: datetime,
) -> None:
    cooldown_text = cooldown_until.isoformat() if cooldown_until is not None else ''
    connection.execute(
        'INSERT INTO discovery_skip_log '
        '(logged_at_utc, ticker, reason, detail, source_rejection_code, cooldown_until_utc) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (
            logged_at_utc.isoformat(),
            ticker,
            reason,
            detail,
            source_rejection_code,
            cooldown_text,
        ),
    )
    if reason in DISCOVERY_COOLDOWN_REASONS and cooldown_until is not None:
        connection.execute(
            'INSERT INTO discovery_symbol_cooldowns '
            '(ticker, reason, detail, cooldown_until_utc, updated_at_utc) '
            'VALUES (?, ?, ?, ?, ?) '
            'ON CONFLICT(ticker) DO UPDATE SET '
            'reason = excluded.reason, detail = excluded.detail, '
            'cooldown_until_utc = excluded.cooldown_until_utc, updated_at_utc = excluded.updated_at_utc',
            (ticker, reason, detail, cooldown_text, logged_at_utc.isoformat()),
        )


def _classify_discovery_skip(row: pd.Series) -> tuple[str, str] | None:
    setup = str(row.get('options_setup', '') or '').strip().lower()
    if setup == 'insufficient_data':
        return 'STALE_QUOTE', 'Insufficient discovery data prevented queueing.'

    code = str(row.get('rejection_code', '') or '').strip().upper()
    detail = str(row.get('rejection_detail', '') or '').strip() or 'Discovery candidate was rejected before queueing.'
    if code == 'REJECTED_SAFETY_DATA_UNAVAILABLE':
        return 'STALE_QUOTE', detail
    if code in {'REJECTED_SPREAD_CONTRACTS', 'REJECTED_SPREAD_EXPIRATION', 'REJECTED_SPREAD_STRIKES'}:
        return 'NO_MATCHING_SPREAD', detail
    if code == 'REJECTED_SPREAD_WIDTH':
        return 'WIDE_SPREAD', detail
    return None


def _apply_discovery_health_gates(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty:
        return candidates

    database = _discovery_db_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    survivors: list[dict[str, object]] = []
    cooldown_minutes = _discovery_cooldown_minutes()

    with closing(sqlite3.connect(database)) as connection, connection:
        _ensure_discovery_tables(connection)
        now_utc = datetime.now(timezone.utc)
        for _, row in candidates.iterrows():
            ticker = str(row.get('ticker', '') or '').strip().upper()
            if not ticker:
                continue
            active_until = _load_active_cooldown_until(connection, ticker, now_utc)
            if active_until is not None:
                _record_discovery_skip(
                    connection=connection,
                    ticker=ticker,
                    reason='COOLDOWN_ACTIVE',
                    detail=f'Symbol cooldown active until {active_until.isoformat()}.',
                    source_rejection_code='',
                    cooldown_until=active_until,
                    logged_at_utc=now_utc,
                )
                continue
            classified = _classify_discovery_skip(row)
            if classified is None:
                survivors.append(row.to_dict())
                continue
            reason, detail = classified
            cooldown_until = now_utc + timedelta(minutes=cooldown_minutes) if cooldown_minutes > 0 and reason in DISCOVERY_COOLDOWN_REASONS else None
            _record_discovery_skip(
                connection=connection,
                ticker=ticker,
                reason=reason,
                detail=detail,
                source_rejection_code=str(row.get('rejection_code', '') or '').strip().upper(),
                cooldown_until=cooldown_until,
                logged_at_utc=now_utc,
            )
    return pd.DataFrame(survivors, columns=candidates.columns)


def _first_numeric(row: pd.Series, columns: list[str]) -> float | None:
    for column in columns:
        value = pd.to_numeric(row.get(column), errors='coerce')
        if pd.notna(value):
            return float(value)
    return None


def _liquidity_score(row: pd.Series) -> float:
    spread = _first_numeric(row, ['option_spread_pct', 'bid_ask_spread_pct', 'spread_pct', 'long_leg_spread_pct', 'short_leg_spread_pct'])
    if spread is None:
        spread_score = 0.5
    else:
        spread_pct = spread / 100.0 if spread > 1 else spread
        spread_pct = max(0.0, float(spread_pct))
        spread_score = max(0.0, 1.0 - min(spread_pct / 0.06, 1.0))
    open_interest = _first_numeric(row, ['open_interest', 'avg_open_interest'])
    oi_score = min(max(open_interest, 0.0) / 2000.0, 1.0) if open_interest is not None else 0.5
    return 0.7 * spread_score + 0.3 * oi_score


def _iv_alignment_score(row: pd.Series) -> float:
    iv_rank = _first_numeric(row, ['iv_rank', 'iv_rank_pct', 'implied_volatility_rank'])
    if iv_rank is None:
        return 0.5
    iv_rank = max(0.0, min(float(iv_rank), 100.0))
    return max(0.0, 1.0 - abs(iv_rank - 50.0) / 50.0)


def _signal_strength_score(row: pd.Series) -> float:
    signal = _first_numeric(row, ['signal_score'])
    if signal is None:
        return 0.0
    return min(abs(signal), 10.0) / 10.0


def _composite_opportunity_score(row: pd.Series) -> float:
    return round(
        100.0 * (
            0.45 * _liquidity_score(row)
            + 0.25 * _iv_alignment_score(row)
            + 0.30 * _signal_strength_score(row)
        ),
        3,
    )


def _append_note(existing: str, note: str) -> str:
    """Append a note once; collapse repeats so comments don't grow every cycle."""
    text = str(existing or '').strip()
    if note in text:
        cut = text.index(note) + len(note)
        text = text[:cut] + text[cut:].replace(note, '')
        return ' '.join(text.split())
    return f'{text} {note}'.strip()


def _recover_stale_previous_state(previous: pd.DataFrame) -> pd.DataFrame:
    recovered = previous.copy()
    recovery_minutes = int(os.getenv('PAPER_QUEUE_RECOVERY_MINUTES', '60') or 60)
    if recovery_minutes <= 0:
        return recovered

    if 'queue_refreshed_at_utc' not in recovered.columns:
        return recovered

    try:
        refreshed = pd.to_datetime(recovered['queue_refreshed_at_utc'], utc=True, errors='coerce')
    except Exception:
        return recovered

    stale_mask = refreshed.notna() & (pd.Timestamp.now(tz='UTC') - refreshed).dt.total_seconds().div(60).gt(recovery_minutes)
    if not stale_mask.any():
        return recovered

    for col, default in {
        'approval_status': 'pending',
        'approved_for_submit': False,
        'order_status': 'queued_for_review',
        'option_order_status': 'queued_for_review',
        'last_submitted_at': '',
        'last_option_submitted_at': '',
    }.items():
        if col in recovered.columns:
            if isinstance(default, str):
                recovered[col] = recovered[col].astype(object)
            recovered.loc[stale_mask, col] = default

    if 'comment' in recovered.columns:
        recovered['comment'] = recovered['comment'].astype(object)
        recovered.loc[stale_mask, 'comment'] = recovered.loc[stale_mask, 'comment'].fillna('').astype(str).map(
            lambda text: _append_note(text, 'Queue recovery reset stale hold for retry.')
        )

    return recovered


def _preserve_existing_state(queue: pd.DataFrame) -> pd.DataFrame:
    """Carry forward in-progress state from the existing queue file.
    Terminal statuses (filled, accepted, pending_new) are intentionally NOT
    preserved — a ticker re-appearing as a new candidate is a fresh trade idea
    and must start with a clean order_status so the execution engine doesn't
    skip it as already submitted.
    """
    out_file = OUTPUT_DIR / 'paper_trade_queue.csv'
    if queue.empty or not out_file.exists():
        return queue

    previous = pd.read_csv(out_file)
    if previous.empty or 'ticker' not in previous.columns:
        return queue

    # Only preserve state for entries that are still mid-flight (not completed)
    _terminal = {'filled', 'accepted', 'pending_new', 'partially_filled', 'submitted', 'new'}
    active_previous = previous.copy()
    if 'order_status' in active_previous.columns:
        active_previous = active_previous[
            ~active_previous['order_status'].astype(str).str.lower().isin(_terminal)
        ]

    active_previous = _recover_stale_previous_state(active_previous)

    for col, default in {
        'approval_status': 'pending',
        'approved_for_submit': False,
        'discovery_quality_override': False,
        'order_status': 'queued_for_review',
        'last_submitted_at': '',
        'option_order_status': 'queued_for_review',
        'last_option_submitted_at': '',
        'comment': 'Review bid/ask spread, open interest, and event risk. Change approval_status to approved only after manual review.',
    }.items():
        if col not in active_previous.columns:
            active_previous[col] = default
        queue[col] = queue['ticker'].map(active_previous.set_index('ticker')[col].to_dict()).fillna(queue.get(col, default))

    return queue


def _load_discovery_quality() -> tuple[dict[str, dict], float]:
    """Load latest discovery quality map keyed by ticker.

    Returns ({ticker: item}, threshold). If unavailable, returns ({}, 0).
    """
    path = WIKI_DIR / 'discovery_quality.json'
    if not path.exists():
        return {}, 0.0
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}, 0.0

    items = {
        str(item.get('ticker', '')).upper(): item
        for item in payload.get('items', [])
        if item.get('ticker')
    }
    return items, float(payload.get('threshold', 0) or 0)


def _apply_discovery_quality_gate(queue: pd.DataFrame) -> pd.DataFrame:
    """Apply discovery quality gating to queue rows.

    Names marked block are forced to hold/pending submit=false. This keeps
    automatic approval from routing low-quality names without an explicit
    manual override path.

    Cross-check: if the DQ JSON blocked a row only because guardrail_status was
    unknown at scoring time but the live queue row now has guardrail_status=pass
    and score >= threshold, treat it as passing. This resolves the circular
    DQ-queue dependency where tickers enter the queue AFTER DQ was last scored.
    """
    if queue.empty:
        return queue

    items, threshold = _load_discovery_quality()
    queue = queue.copy()
    queue['discovery_quality_score'] = 0.0
    queue['discovery_quality_status'] = 'unknown'
    queue['discovery_quality_formula_version'] = ''
    queue['discovery_quality_gate_reason'] = ''

    for idx, row in queue.iterrows():
        ticker = str(row.get('ticker', '')).upper()
        item = items.get(ticker)
        if not item:
            continue

        score = float(item.get('discovery_quality_score', 0.0) or 0.0)
        status = str(item.get('status', 'unknown')).strip().lower() or 'unknown'
        formula = str(item.get('formula_version', ''))
        reasons = item.get('meta', {}).get('blocked_reasons', [])
        reason_text = '; '.join(str(r) for r in reasons if str(r).strip())

        # Cross-check: if DQ blocked solely because guardrail was unknown at
        # scoring time but the live queue row already has guardrail=pass and
        # the numeric score clears the threshold, lift the block here.
        live_guardrail = str(row.get('guardrail_status', '')).strip().lower()
        dq_guardrail_blocked = 'guardrail_status' in reason_text and 'unknown' in reason_text
        if status == 'block' and dq_guardrail_blocked and live_guardrail == 'pass' and score >= threshold:
            status = 'pass'
            reason_text = ''

        queue.at[idx, 'discovery_quality_score'] = score
        queue.at[idx, 'discovery_quality_status'] = status
        queue.at[idx, 'discovery_quality_formula_version'] = formula
        queue.at[idx, 'discovery_quality_gate_reason'] = reason_text

        override = str(row.get('discovery_quality_override', '')).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
        strict_mode = str(os.getenv('DISCOVERY_QUALITY_STRICT', 'true')).strip().lower() not in {'0', 'false', 'no', 'off'}
        auto_approve = str(os.getenv('AUTO_APPROVE_PAPER_TRADES', 'false')).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
        blocked = (status == 'block' or (threshold > 0 and score < threshold))

        # If the DQ gate now passes for a row that was previously held because of the
        # DQ gate (not a manual hold), lift the hold back to pending so auto-approval
        # can pick it up in the next execution cycle.
        if not blocked:
            current_status = str(row.get('approval_status', 'pending')).strip().lower()
            current_comment = str(row.get('comment', '') or '')
            dq_gate_was_cause = 'Discovery quality gate blocked routing' in current_comment
            if current_status == 'hold' and dq_gate_was_cause:
                queue.at[idx, 'approval_status'] = 'pending'
                queue.at[idx, 'approved_for_submit'] = False
                queue.at[idx, 'comment'] = current_comment.replace(
                    'Discovery quality gate blocked routing: ' + (reason_text or f'discovery quality below threshold {threshold:.0f}') + '.', ''
                ).strip()


        if blocked and not override and not (auto_approve and not strict_mode):
            queue.at[idx, 'approval_status'] = 'hold'
            queue.at[idx, 'approved_for_submit'] = False
            existing = str(queue.at[idx, 'comment']) if 'comment' in queue.columns else ''
            gate_reason = reason_text or f'discovery quality below threshold {threshold:.0f}'
            gate_note = f'Discovery quality gate blocked routing: {gate_reason}.'
            queue.at[idx, 'comment'] = _append_note(existing, gate_note)
        elif blocked and override:
            existing = str(queue.at[idx, 'comment']) if 'comment' in queue.columns else ''
            gate_reason = reason_text or f'discovery quality below threshold {threshold:.0f}'
            queue.at[idx, 'comment'] = (
                f"{existing} Discovery quality override enabled; blocked reason: {gate_reason}."
            ).strip()
        elif blocked and auto_approve and not strict_mode:
            existing = str(queue.at[idx, 'comment']) if 'comment' in queue.columns else ''
            queue.at[idx, 'comment'] = (
                f"{existing} Discovery quality gate is relaxed for safe auto-approval mode."
            ).strip()

    return queue


def build_paper_trade_queue(
    candidates: pd.DataFrame,
    max_positions: int = 8,
    min_signal_score: int = 3,
    min_projected_return: float = 5.0,
    existing_tickers: set[str] | None = None,
    positions: pd.DataFrame | None = None,
    account_size_usd: float = 100_000.0,
    max_total_exposure_pct: float = 35.0,
    max_single_name_exposure_pct: float = 8.0,
    max_queue_risk_usd: float = 5_000.0,
    max_sector_exposure_pct: float = 100.0,
    max_correlation_bucket_exposure_pct: float = 100.0,
    max_options_exposure_pct: float = 20.0,
    discovery_top_k: int | None = None,
) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()

    candidates = _apply_discovery_health_gates(candidates)
    if candidates.empty:
        return pd.DataFrame()

    rejection_codes = candidates.get('rejection_code', pd.Series('', index=candidates.index)).fillna('').astype(str)
    rejected = candidates.loc[rejection_codes.str.startswith('REJECTED_')].copy()
    if not rejected.empty:
        defaults = {
            'signal_close': 0.0, 'underlying_midpoint': 0.0, 'intraday_drift_pct': 0.0,
            'spread_type': '', 'spread_long_strike': None, 'spread_short_strike': None,
            'spread_option_type': '', 'spread_ratio_qty': 0, 'expiration_target': '', 'days_to_expiration': None,
            'rejection_detail': '', 'catalyst_checked_at_utc': '', 'catalyst_status': 'UNKNOWN',
            'catalyst_reason': '', 'earnings_dates': '', 'headline_count_60m': None,
            'discovery_quality_score': 0.0, 'discovery_quality_status': 'unknown',
            'discovery_quality_formula_version': '', 'discovery_quality_gate_reason': '',
            'current_name_exposure_pct': 0.0, 'portfolio_exposure_after_trade_pct': 0.0,
            'sector': '', 'correlation_bucket': '', 'asset_family': '',
            'current_sector_exposure_pct': 0.0, 'sector_exposure_after_trade_pct': 0.0,
            'current_bucket_exposure_pct': 0.0, 'bucket_exposure_after_trade_pct': 0.0,
            'discovery_composite_score': 0.0,
        }
        for column, default in defaults.items():
            if column not in rejected.columns:
                rejected[column] = default
        rejected['broker_route'] = 'alpaca-paper'
        rejected['order_status'] = 'blocked'
        rejected['option_order_status'] = 'blocked'
        rejected['approval_status'] = 'hold'
        rejected['approved_for_submit'] = False
        rejected['discovery_quality_override'] = False
        rejected['last_submitted_at'] = ''
        rejected['last_option_submitted_at'] = ''
        rejected['queue_refreshed_at_utc'] = datetime.now(timezone.utc).isoformat()
        rejected['guardrail_status'] = 'blocked'
        rejected['guardrail_reason'] = rejected['rejection_detail'].fillna('')
        rejected['comment'] = rejected['rejection_detail'].fillna('')

    queue = candidates.copy()
    queue = queue[
        rejection_codes.eq('')
        & queue['options_setup'].ne('No trade')
        & queue['signal_score'].ge(min_signal_score)
        & queue['projected_return_pct'].ge(min_projected_return)
        & queue['allocation_pct'].gt(0)
    ].copy()

    existing_source = existing_tickers if existing_tickers is not None else load_existing_tickers()
    existing = {str(ticker).upper() for ticker in existing_source if str(ticker).strip()}
    if existing:
        queue = queue[~queue['ticker'].astype(str).str.upper().isin(existing)].copy()

    if queue.empty:
        return rejected

    for column, default in {
        'signal_close': 0.0,
        'spread_type': '',
        'spread_long_strike': None,
        'spread_short_strike': None,
        'spread_option_type': '',
        'spread_ratio_qty': 0,
        'expiration_target': '',
        'days_to_expiration': None,
        'underlying_midpoint': 0.0,
        'intraday_drift_pct': 0.0,
        'rejection_code': '',
        'rejection_detail': '',
        'catalyst_checked_at_utc': '',
        'catalyst_status': 'UNKNOWN',
        'catalyst_reason': '',
        'earnings_dates': '',
        'headline_count_60m': None,
        'discovery_composite_score': 0.0,
    }.items():
        if column not in queue.columns:
            queue[column] = default

    queue = apply_exposure_guardrails(
        queue,
        positions=positions,
        account_size_usd=account_size_usd,
        max_total_exposure_pct=max_total_exposure_pct,
        max_single_name_exposure_pct=max_single_name_exposure_pct,
        max_queue_risk_usd=max_queue_risk_usd,
        max_positions=max_positions,
        max_sector_exposure_pct=max_sector_exposure_pct,
        max_correlation_bucket_exposure_pct=max_correlation_bucket_exposure_pct,
        max_options_exposure_pct=max_options_exposure_pct,
    )
    if queue.empty:
        if not rejected.empty:
            return rejected
        return pd.DataFrame(columns=[
            'ticker', 'bias', 'options_setup', 'signal_score', 'projected_return_pct', 'max_risk_usd',
            'allocation_pct', 'broker_route', 'order_status', 'approval_status', 'approved_for_submit',
            'last_submitted_at', 'comment', 'option_order_status', 'last_option_submitted_at',
            'queue_refreshed_at_utc', 'guardrail_status', 'guardrail_reason',
            'current_name_exposure_pct', 'portfolio_exposure_after_trade_pct',
            'sector', 'correlation_bucket', 'asset_family', 'current_sector_exposure_pct',
            'sector_exposure_after_trade_pct', 'current_bucket_exposure_pct', 'bucket_exposure_after_trade_pct',
            'discovery_composite_score',
        ])

    queue['discovery_composite_score'] = queue.apply(_composite_opportunity_score, axis=1)
    top_k = _discovery_top_k() if discovery_top_k is None else max(1, int(discovery_top_k))
    queue = queue.sort_values(
        ['discovery_composite_score', 'rank_score', 'signal_score', 'projected_return_pct'],
        ascending=[False, False, False, False],
    ).head(min(max_positions, top_k)).copy()

    queue['broker_route'] = 'alpaca-paper'
    queue['order_status'] = 'queued_for_review'
    queue['approval_status'] = 'pending'
    queue['approved_for_submit'] = False
    queue['discovery_quality_override'] = False
    queue['last_submitted_at'] = ''
    queue['option_order_status'] = 'queued_for_review'
    queue['last_option_submitted_at'] = ''
    queue['queue_refreshed_at_utc'] = datetime.now(timezone.utc).isoformat()
    queue['comment'] = queue.get('guardrail_reason', 'Review bid/ask spread, open interest, and event risk. Change approval_status to approved only after manual review.')
    queue = _preserve_existing_state(queue)
    queue = _apply_discovery_quality_gate(queue)
    for column in ('rejection_code', 'rejection_detail'):
        queue[column] = queue[column].astype(object)
    catalyst_blocked = queue['catalyst_status'].fillna('UNKNOWN').astype(str).str.upper().eq('MANUAL_REVIEW_REQUIRED')
    queue.loc[catalyst_blocked, 'approval_status'] = 'hold'
    queue.loc[catalyst_blocked, 'approved_for_submit'] = False
    queue.loc[catalyst_blocked, 'rejection_code'] = 'REJECTED_CATALYST_BLACKOUT'
    queue.loc[catalyst_blocked, 'rejection_detail'] = queue.loc[catalyst_blocked, 'catalyst_reason'].fillna('')
    if not rejected.empty:
        queue = pd.concat([queue, rejected], ignore_index=True)
    return queue[
        [
            'ticker',
            'bias',
            'signal_close',
            'spread_type',
            'spread_long_strike',
            'spread_short_strike',
            'spread_option_type',
            'spread_ratio_qty',
            'expiration_target',
            'days_to_expiration',
            'underlying_midpoint',
            'intraday_drift_pct',
            'rejection_code',
            'rejection_detail',
            'catalyst_checked_at_utc',
            'catalyst_status',
            'catalyst_reason',
            'earnings_dates',
            'headline_count_60m',
            'options_setup',
            'signal_score',
            'projected_return_pct',
            'max_risk_usd',
            'allocation_pct',
            'broker_route',
            'order_status',
            'approval_status',
            'approved_for_submit',
            'discovery_quality_override',
            'last_submitted_at',
            'comment',
            'option_order_status',
            'last_option_submitted_at',
            'queue_refreshed_at_utc',
            'guardrail_status',
            'guardrail_reason',
            'discovery_quality_score',
            'discovery_quality_status',
            'discovery_quality_formula_version',
            'discovery_quality_gate_reason',
            'discovery_composite_score',
            'current_name_exposure_pct',
            'portfolio_exposure_after_trade_pct',
            'sector',
            'correlation_bucket',
            'asset_family',
            'current_sector_exposure_pct',
            'sector_exposure_after_trade_pct',
            'current_bucket_exposure_pct',
            'bucket_exposure_after_trade_pct',
        ]
    ]


def main() -> None:
    _load_env_file()
    candidates = load_candidates()
    positions = load_positions_snapshot()

    account_size_usd = float(os.getenv('PAPER_ACCOUNT_SIZE', '100000') or 100000)
    max_positions = int(os.getenv('MAX_OPEN_POSITIONS', '8') or 8)
    # Total exposure limit only counts OPTIONS positions — stock holdings are a separate strategy
    max_total_exposure_pct = float(os.getenv('MAX_TOTAL_EXPOSURE_PCT', '40') or 40)
    max_single_name_exposure_pct = float(os.getenv('MAX_SINGLE_NAME_EXPOSURE_PCT', '8') or 8)
    max_queue_risk_usd = float(os.getenv('MAX_QUEUE_RISK_USD', '5000') or 5000)
    max_sector_exposure_pct = float(os.getenv('MAX_SECTOR_EXPOSURE_PCT', '15') or 15)
    max_correlation_bucket_exposure_pct = float(os.getenv('MAX_CORRELATION_BUCKET_EXPOSURE_PCT', '18') or 18)
    max_options_exposure_pct = float(os.getenv('MAX_OPTIONS_EXPOSURE_PCT', '40') or 40)
    discovery_top_k = _discovery_top_k()

    # Only pass options positions to guardrails — stock holdings don't crowd out options spreads
    options_positions = pd.DataFrame(columns=['symbol', 'market_value'])
    if not positions.empty and 'symbol' in positions.columns:
        options_positions = positions[positions['symbol'].astype(str).apply(_is_option_symbol)].copy()

    queue = build_paper_trade_queue(
        candidates,
        max_positions=max_positions,
        positions=options_positions,
        account_size_usd=account_size_usd,
        max_total_exposure_pct=max_total_exposure_pct,
        max_single_name_exposure_pct=max_single_name_exposure_pct,
        max_queue_risk_usd=max_queue_risk_usd,
        max_sector_exposure_pct=max_sector_exposure_pct,
        max_correlation_bucket_exposure_pct=max_correlation_bucket_exposure_pct,
        max_options_exposure_pct=max_options_exposure_pct,
        discovery_top_k=discovery_top_k,
    )
    exposure_summary = build_exposure_summary(positions, account_size_usd=account_size_usd)
    risk_overview = build_risk_overview(positions, account_size_usd=account_size_usd)
    stress_table = build_stress_test_table(positions)
    save_exposure_summary(Path(__file__).resolve().parent, exposure_summary)
    save_risk_outputs(Path(__file__).resolve().parent, risk_overview, stress_table)

    if queue.empty:
        print('No paper-trade candidates passed the risk gate.')
        return

    out_file = OUTPUT_DIR / 'paper_trade_queue.csv'
    queue.to_csv(out_file, index=False)

    print('Paper-trade queue generated:')
    print(out_file)
    print()
    print(queue.to_string(index=False))


if __name__ == '__main__':
    main()
