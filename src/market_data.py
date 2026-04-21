from __future__ import annotations

from typing import Dict, Iterable

import pandas as pd
import yfinance as yf


DEFAULT_TICKERS = [
    'SPY', 'QQQ', 'IWM', 'DIA', 'XLK', 'XLF', 'XLE', 'XLV', 'SMH',
    'NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'AMD', 'TSLA', 'NFLX', 'PLTR',
    'CRM', 'AVGO', 'JPM', 'BAC', 'GS', 'XOM', 'CVX', 'LLY', 'UNH', 'DIS', 'PYPL',
    'INTC', 'UBER', 'SNOW', 'ROKU', 'SNAP', 'ACN'
]


def _normalize_download_frame(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        if ticker in raw.columns.get_level_values(-1):
            df = raw.xs(ticker, axis=1, level=-1)
        else:
            df = raw.copy()
    else:
        df = raw.copy()

    df = df.reset_index()
    return df.dropna().copy()


def fetch_history(tickers: Iterable[str], period: str = '6mo', interval: str = '1d') -> Dict[str, pd.DataFrame]:
    results: Dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            raw = yf.download(ticker, period=period, interval=interval, auto_adjust=True, progress=False)
            results[ticker] = _normalize_download_frame(raw, ticker)
        except Exception:
            results[ticker] = pd.DataFrame()
    return results
