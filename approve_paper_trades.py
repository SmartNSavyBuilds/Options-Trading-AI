from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


OUTPUT_DIR = Path(__file__).resolve().parent / 'outputs'
QUEUE_FILE = OUTPUT_DIR / 'paper_trade_queue.csv'


def _apply_discovery_quality_hard_gate(df: pd.DataFrame) -> pd.DataFrame:
    if 'discovery_quality_status' not in df.columns:
        df['discovery_quality_status'] = 'unknown'
    if 'discovery_quality_override' not in df.columns:
        df['discovery_quality_override'] = False

    blocked_mask = (
        df['discovery_quality_status'].astype(str).str.lower().eq('block')
        & ~df['discovery_quality_override'].fillna(False).astype(bool)
    )
    df.loc[blocked_mask, 'approval_status'] = 'hold'
    df.loc[blocked_mask, 'approved_for_submit'] = False
    return df


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Approve paper-trade candidates for broker submission.')
    parser.add_argument('--tickers', nargs='*', default=[], help='Ticker symbols to approve, for example: --tickers QQQ SPY')
    parser.add_argument('--all', action='store_true', help='Approve all queued ideas.')
    parser.add_argument('--reset', action='store_true', help='Reset all approvals back to pending.')
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not QUEUE_FILE.exists():
        print('Queue file not found. Run paper_trade.py first.')
        return

    df = pd.read_csv(QUEUE_FILE)
    if 'approval_status' not in df.columns:
        df['approval_status'] = 'pending'
    if 'approved_for_submit' not in df.columns:
        df['approved_for_submit'] = False

    if args.reset:
        df['approval_status'] = 'pending'
        df['approved_for_submit'] = False
        df = _apply_discovery_quality_hard_gate(df)
        df.to_csv(QUEUE_FILE, index=False)
        print('All paper trades were reset to pending review.')
        return

    if args.all:
        df['approval_status'] = 'approved'
        df['approved_for_submit'] = True
        df = _apply_discovery_quality_hard_gate(df)
        df.to_csv(QUEUE_FILE, index=False)
        print('All queued paper trades were approved.')
        print(df.to_string(index=False))
        return

    tickers = {ticker.upper() for ticker in args.tickers}
    if not tickers:
        print('No tickers were provided. Use --tickers or --all.')
        return

    mask = df['ticker'].astype(str).str.upper().isin(tickers)
    if not mask.any():
        print('No matching queued tickers were found.')
        return

    df.loc[mask, 'approval_status'] = 'approved'
    df.loc[mask, 'approved_for_submit'] = True
    df = _apply_discovery_quality_hard_gate(df)
    df.to_csv(QUEUE_FILE, index=False)

    print('Approved trades:')
    print(df.loc[mask].to_string(index=False))


if __name__ == '__main__':
    main()
