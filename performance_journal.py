from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.performance_journal import build_performance_journal, save_performance_outputs


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'


def _load_latest_output(prefix: str) -> pd.DataFrame:
    matches = sorted(OUTPUT_DIR.glob(f'{prefix}*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        return pd.DataFrame()
    return pd.read_csv(matches[0])


def main() -> None:
    positions = _load_latest_output('broker_positions')
    execution_log = _load_latest_output('execution_log')
    exits = _load_latest_output('exit_recommendations')
    journal = build_performance_journal(positions, execution_log)
    journal_path, summary_path, alerts_path, attribution_path, quality_path, learning_path = save_performance_outputs(PROJECT_DIR, journal, exits, execution_log)

    print('Performance journal generated:')
    print(journal_path)
    print(summary_path)
    print(alerts_path)
    print(attribution_path)
    print(quality_path)
    print(learning_path)
    print()
    if journal.empty:
        print('No open positions were available for journal generation.')
    else:
        print(journal.to_string(index=False))


if __name__ == '__main__':
    main()
