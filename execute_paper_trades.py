from __future__ import annotations

from bootstrap_path import ensure_project_root_on_path

ensure_project_root_on_path()

from src.execution import (
    TradingConfig,
    append_execution_log,
    run_option_execution_cycle,
    save_execution_preview,
    sync_broker_state,
)


def _is_options_contracts_auth_error(error: Exception) -> bool:
    text = str(error or '').lower()
    return '/v2/options/contracts' in text and ('401' in text or 'unauthorized' in text)


def main() -> None:
    config = TradingConfig.from_env()
    account, positions = sync_broker_state(config)
    # Route through real options contracts (single-leg, chosen by the signal's
    # bias and strike hint). This replaces the old proxy-equity path so Alpaca
    # receives actual OCC option symbols instead of the underlying stock ticker.
    # Route within configured DTE guardrails to avoid near-expiry contracts.
    # top_n=5 allows up to 5 new positions per cycle.
    try:
        preview, results = run_option_execution_cycle(
            config,
            min_days=config.min_option_dte,
            max_days=config.max_option_dte,
            top_n=5,
        )
    except Exception as exc:
        if _is_options_contracts_auth_error(exc):
            print(
                'Option contract lookup returned an auth error; '
                'skipping entry routing for this cycle to keep the worker healthy.'
            )
            print(f'Detail: {str(exc)[:300]}')
            return
        raise

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
