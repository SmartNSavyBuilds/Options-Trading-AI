from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.execution import _load_env_file
from src.risk_guardrails import apply_exposure_guardrails, build_exposure_summary, build_risk_overview, build_stress_test_table, save_exposure_summary, save_risk_outputs


OUTPUT_DIR = Path(__file__).resolve().parent / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)


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
            active_statuses = {'new', 'accepted', 'filled', 'partially_filled', 'submitted', 'pending_new'}
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
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
    return df


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

    for col, default in {
        'approval_status': 'pending',
        'approved_for_submit': False,
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
) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()

    queue = candidates.copy()
    queue = queue[
        queue['options_setup'].ne('No trade')
        & queue['signal_score'].ge(min_signal_score)
        & queue['projected_return_pct'].ge(min_projected_return)
        & queue['allocation_pct'].gt(0)
    ].copy()

    existing = {str(ticker).upper() for ticker in (existing_tickers or load_existing_tickers()) if str(ticker).strip()}
    if existing:
        queue = queue[~queue['ticker'].astype(str).str.upper().isin(existing)].copy()

    if queue.empty:
        return pd.DataFrame()

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
        return pd.DataFrame(columns=[
            'ticker', 'bias', 'options_setup', 'signal_score', 'projected_return_pct', 'max_risk_usd',
            'allocation_pct', 'broker_route', 'order_status', 'approval_status', 'approved_for_submit',
            'last_submitted_at', 'comment', 'option_order_status', 'last_option_submitted_at',
            'queue_refreshed_at_utc', 'guardrail_status', 'guardrail_reason',
            'current_name_exposure_pct', 'portfolio_exposure_after_trade_pct',
            'sector', 'correlation_bucket', 'asset_family', 'current_sector_exposure_pct',
            'sector_exposure_after_trade_pct', 'current_bucket_exposure_pct', 'bucket_exposure_after_trade_pct'
        ])

    queue['broker_route'] = 'alpaca-paper'
    queue['order_status'] = 'queued_for_review'
    queue['approval_status'] = 'pending'
    queue['approved_for_submit'] = False
    queue['last_submitted_at'] = ''
    queue['option_order_status'] = 'queued_for_review'
    queue['last_option_submitted_at'] = ''
    queue['queue_refreshed_at_utc'] = datetime.now(timezone.utc).isoformat()
    queue['comment'] = queue.get('guardrail_reason', 'Review bid/ask spread, open interest, and event risk. Change approval_status to approved only after manual review.')
    queue = _preserve_existing_state(queue)
    return queue[
        [
            'ticker',
            'bias',
            'options_setup',
            'signal_score',
            'projected_return_pct',
            'max_risk_usd',
            'allocation_pct',
            'broker_route',
            'order_status',
            'approval_status',
            'approved_for_submit',
            'last_submitted_at',
            'comment',
            'option_order_status',
            'last_option_submitted_at',
            'queue_refreshed_at_utc',
            'guardrail_status',
            'guardrail_reason',
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
