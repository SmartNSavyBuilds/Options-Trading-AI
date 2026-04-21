from __future__ import annotations

from src.execution import TradingConfig, sync_broker_state


def main() -> None:
    config = TradingConfig.from_env()
    account, positions = sync_broker_state(config)

    print('Broker account status:')
    print(account.to_string(index=False))
    print()

    if positions.empty:
        print('No broker positions are currently open or sync is unavailable.')
    else:
        print('Broker positions:')
        print(positions.to_string(index=False))


if __name__ == '__main__':
    main()
