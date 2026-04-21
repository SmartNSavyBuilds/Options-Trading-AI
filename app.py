from datetime import datetime
from pathlib import Path

from src.market_data import DEFAULT_TICKERS, fetch_history
from src.options_selector import build_options_candidates
from src.signal_engine import build_signal_report


OUTPUT_DIR = Path(__file__).resolve().parent / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)


def _safe_write_csv(df, path: Path) -> Path:
    try:
        df.to_csv(path, index=False)
        return path
    except PermissionError:
        fallback = path.with_name(f"{path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}")
        df.to_csv(fallback, index=False)
        return fallback


def main() -> None:
    market_data = fetch_history(DEFAULT_TICKERS, period='6mo', interval='1d')
    report = build_signal_report(market_data)
    candidates = build_options_candidates(report)

    out_file = OUTPUT_DIR / 'latest_signals.csv'
    candidates_file = OUTPUT_DIR / 'options_candidates.csv'

    out_file = _safe_write_csv(report, out_file)
    candidates_file = _safe_write_csv(candidates, candidates_file)

    print('Generated signal report:')
    print(out_file)
    print('Generated options candidates:')
    print(candidates_file)
    print()
    print(report.to_string(index=False))
    print()
    print(candidates.to_string(index=False))


if __name__ == '__main__':
    main()
