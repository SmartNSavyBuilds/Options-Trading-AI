from __future__ import annotations

from pathlib import Path

from src.multi_asset import generate_multi_asset_outputs


PROJECT_DIR = Path(__file__).resolve().parent


def main() -> None:
    watchlist_path, regime_path, watchlist, regime = generate_multi_asset_outputs(PROJECT_DIR)

    print('Multi-asset outputs generated:')
    print(watchlist_path)
    print(regime_path)
    print()

    if watchlist.empty:
        print('Crypto watchlist is empty right now. Data may be unavailable or no assets passed the filter.')
    else:
        print('Crypto watchlist:')
        print(watchlist.to_string(index=False))

    print()
    print('Market regime summary:')
    print(regime.to_string(index=False))


if __name__ == '__main__':
    main()
