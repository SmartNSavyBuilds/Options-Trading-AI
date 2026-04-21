from pathlib import Path

from src.backtest import backtest_directional_signals
from src.market_data import DEFAULT_TICKERS, fetch_history


OUTPUT_DIR = Path(__file__).resolve().parent / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)


def main() -> None:
    market_data = fetch_history(DEFAULT_TICKERS, period='1y', interval='1d')
    report = backtest_directional_signals(market_data)
    out_file = OUTPUT_DIR / 'backtest_summary.csv'
    report.to_csv(out_file, index=False)

    print('Generated backtest summary:')
    print(out_file)
    print()
    print(report.to_string(index=False))


if __name__ == '__main__':
    main()
