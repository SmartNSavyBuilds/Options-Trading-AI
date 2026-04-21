from __future__ import annotations

import argparse
import os

from src.execution import (
    TradingConfig,
    append_execution_log,
    run_option_execution_cycle,
    save_execution_preview,
    sync_broker_state,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Route approved short-dated option ideas to Alpaca paper trading.')
    parser.add_argument('--top-n', type=int, default=int(os.getenv('MAX_OPTION_POSITIONS', '4')), help='How many option ideas to route on this cycle.')
    parser.add_argument('--max-days', type=int, default=int(os.getenv('MAX_OPTION_DAYS', '10')), help='Maximum days to expiration for contract selection.')
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = TradingConfig.from_env()
    config.use_proxy_equities = False
    account, positions = sync_broker_state(config)
    preview, results = run_option_execution_cycle(config, max_days=max(args.max_days, 1), top_n=max(args.top_n, 1))

    if preview.empty:
        print('No approved short-dated option contracts are ready for routing.')
        return

    preview_path = save_execution_preview(preview)
    results_path = append_execution_log(results)
    post_account, post_positions = sync_broker_state(config)

    print('Broker account snapshot before option execution:')
    print(account.to_string(index=False))
    if not positions.empty:
        print()
        print('Broker positions before option execution:')
        print(positions.to_string(index=False))

    print()
    print('Options execution preview generated:')
    print(preview_path)
    print(preview.to_string(index=False))

    print()
    print('Options execution results:')
    print(results_path)
    if not results.empty:
        print(results.to_string(index=False))

    print()
    print('Broker account snapshot after option execution:')
    print(post_account.to_string(index=False))
    if not post_positions.empty:
        print()
        print('Broker positions after option execution:')
        print(post_positions.to_string(index=False))


if __name__ == '__main__':
    main()
