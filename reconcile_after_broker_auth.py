from __future__ import annotations

from bootstrap_path import ensure_project_root_on_path

ensure_project_root_on_path()

from run_deterministic_pipeline import run_pipeline
from src.execution import TradingConfig, sync_broker_state


def main() -> None:
    config = TradingConfig.from_env()
    account, _positions = sync_broker_state(config)

    if account.empty:
        print('Broker sync did not return an account snapshot. Reconciliation was not started.')
        return

    row = account.iloc[0]
    connection_status = str(row.get('connection_status', 'unknown')).strip().lower()
    detail = str(row.get('detail', '')).strip()

    if connection_status != 'connected':
        print(f'Broker auth is not healthy ({connection_status}).')
        print(detail)
        return

    print('Broker auth is healthy. Launching deterministic recovery pipeline.')
    run_pipeline(require_connected=True, strict_gate=False)


if __name__ == '__main__':
    main()