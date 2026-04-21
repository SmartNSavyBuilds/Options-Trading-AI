from __future__ import annotations

from src.execution import (
    TradingConfig,
    append_execution_log,
    run_option_execution_cycle,
    save_execution_preview,
    sync_broker_state,
)


def main() -> None:
    config = TradingConfig.from_env()
    account, positions = sync_broker_state(config)
    # Route through real options contracts (single-leg, chosen by the signal's
    # bias and strike hint). This replaces the old proxy-equity path so Alpaca
    # receives actual OCC option symbols instead of the underlying stock ticker.
    # max_days=35 aligns with the scorer's ~24-day expiry recommendations.
    # top_n=5 allows up to 5 new positions per cycle.
    preview, results = run_option_execution_cycle(config, max_days=35, top_n=5)

    if preview.empty:
        print('No execution candidates are available. Generate signals and the paper-trade queue first.')
        return

    preview_path = save_execution_preview(preview)
    results_path = append_execution_log(results)
    post_account, post_positions = sync_broker_state(config)

    print('Broker account snapshot before execution:')
    print()
    print(account.to_string(index=False))
    if not positions.empty:
        print()
        print('Broker positions before execution:')
        print(positions.to_string(index=False))
    print()
    print('Execution preview generated:')
    print(preview_path)
    print()
    print(preview.to_string(index=False))
    print()
    print('Execution results:')
    print(results_path)
    print()
    print(results.to_string(index=False))

    print()
    print('Broker account snapshot after execution:')
    print(post_account.to_string(index=False))
    if not post_positions.empty:
        print()
        print('Broker positions after execution:')
        print(post_positions.to_string(index=False))


if __name__ == '__main__':
    main()
