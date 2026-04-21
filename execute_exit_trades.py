from __future__ import annotations

from src.execution import (
    TradingConfig,
    append_execution_log,
    run_exit_execution_cycle,
    save_exit_execution_preview,
    sync_broker_state,
)


def main() -> None:
    config = TradingConfig.from_env()
    account, positions = sync_broker_state(config)
    preview, results = run_exit_execution_cycle(config)

    if preview.empty:
        print('No actionable exit orders are available. Review the current exit recommendations first.')
        return

    preview_path = save_exit_execution_preview(preview)
    results_path = append_execution_log(results) if not results.empty else None
    post_account, post_positions = sync_broker_state(config)

    print('Broker account snapshot before exit routing:')
    print(account.to_string(index=False))
    if not positions.empty:
        print()
        print('Broker positions before exit routing:')
        print(positions.to_string(index=False))

    print()
    print('Exit execution preview generated:')
    print(preview_path)
    print(preview.to_string(index=False))

    print()
    print('Exit execution results log:')
    print(results_path)
    if not results.empty:
        print(results.to_string(index=False))

    print()
    print('Broker account snapshot after exit routing:')
    print(post_account.to_string(index=False))
    if not post_positions.empty:
        print()
        print('Broker positions after exit routing:')
        print(post_positions.to_string(index=False))


if __name__ == '__main__':
    main()
