from __future__ import annotations

from pathlib import Path

from src.congressional_disclosures import import_raw_trade_data, refresh_congressional_outputs


PROJECT_DIR = Path(__file__).resolve().parent


def main() -> None:
    imported = import_raw_trade_data(PROJECT_DIR / 'data')
    sources, disclosures, summary = refresh_congressional_outputs(PROJECT_DIR)

    print('Congressional raw import completed.')
    print(f'Imported rows: {len(imported)}')
    print('Refresh outputs generated for sources, disclosures, summary, recent large-trade directory, and watchlist views.')
    print()
    if disclosures.empty:
        print('No disclosure rows are loaded yet. Place raw CSV or JSON exports in data/congress_raw and rerun this command.')
    else:
        print(disclosures.to_string(index=False))
        print()
        print('Ticker summary:')
        print(summary.to_string(index=False))

    print()
    print('Official source catalog:')
    print(sources.to_string(index=False))


if __name__ == '__main__':
    main()
