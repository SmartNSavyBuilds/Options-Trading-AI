from __future__ import annotations

from pathlib import Path

from src.congressional_disclosures import refresh_congressional_outputs


PROJECT_DIR = Path(__file__).resolve().parent


def main() -> None:
    sources, disclosures, summary = refresh_congressional_outputs(PROJECT_DIR)

    print('Congressional disclosure sources refreshed.')
    print()
    print(sources.to_string(index=False))
    print()
    if disclosures.empty:
        print('No local congressional trade rows are loaded yet. Add rows to data/congress_disclosures.csv to populate the dashboard summaries.')
    else:
        print(disclosures.to_string(index=False))
        print()
        print('Ticker summary:')
        print(summary.to_string(index=False))


if __name__ == '__main__':
    main()
