from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


OUTPUT_DIR = Path(__file__).resolve().parent / 'outputs'
EXIT_FILE = OUTPUT_DIR / 'exit_recommendations.csv'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Approve exit recommendations for guarded paper automation.')
    parser.add_argument('--symbols', nargs='*', default=[], help='Symbols to approve for exit routing, for example: --symbols SPY QQQ')
    parser.add_argument('--all', action='store_true', help='Approve all non-hold exit recommendations.')
    parser.add_argument('--reset', action='store_true', help='Reset all exit approvals back to monitor mode.')
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not EXIT_FILE.exists():
        print('Exit recommendations file not found. Run evaluate_exit_rules.py first.')
        return

    df = pd.read_csv(EXIT_FILE)
    if df.empty:
        print('No exit recommendations are available yet.')
        return

    if 'exit_approved' not in df.columns:
        df['exit_approved'] = False
    if 'order_status' not in df.columns:
        df['order_status'] = 'monitor'

    actionable = df['action'].astype(str).str.lower() != 'hold'

    if args.reset:
        df['exit_approved'] = False
        df['order_status'] = 'monitor'
        df.to_csv(EXIT_FILE, index=False)
        print('All exit approvals were reset.')
        return

    if args.all:
        df.loc[actionable, 'exit_approved'] = True
        df.loc[actionable, 'order_status'] = 'approved_for_review'
        df.to_csv(EXIT_FILE, index=False)
        print('All actionable exits were approved.')
        print(df.loc[actionable].to_string(index=False))
        return

    symbols = {symbol.upper() for symbol in args.symbols}
    if not symbols:
        print('No symbols were provided. Use --symbols or --all.')
        return

    mask = df['symbol'].astype(str).str.upper().isin(symbols) & actionable
    if not mask.any():
        print('No matching actionable exit rows were found.')
        return

    df.loc[mask, 'exit_approved'] = True
    df.loc[mask, 'order_status'] = 'approved_for_review'
    df.to_csv(EXIT_FILE, index=False)

    print('Approved exits:')
    print(df.loc[mask].to_string(index=False))


if __name__ == '__main__':
    main()
