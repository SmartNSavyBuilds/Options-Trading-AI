from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, Iterable

import pandas as pd
import yfinance as yf


DEFAULT_TICKERS = [
    'SPY', 'QQQ', 'IWM', 'DIA', 'XLK', 'XLF', 'XLE', 'XLV', 'SMH',
    'NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'AMD', 'TSLA', 'NFLX', 'PLTR',
    'CRM', 'AVGO', 'JPM', 'BAC', 'GS', 'XOM', 'CVX', 'LLY', 'UNH', 'DIS', 'PYPL',
    'INTC', 'UBER', 'SNOW', 'ROKU', 'SNAP', 'ACN'
]


def stock_quote_midpoint(client, ticker: str, max_age_seconds: float = 60.0) -> float:
    from alpaca.data.requests import StockLatestQuoteRequest

    symbol = str(ticker).strip().upper()
    if not symbol or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError('A ticker and positive quote freshness limit are required')
    quote = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
    bid = float(quote.bid_price)
    ask = float(quote.ask_price)
    timestamp = quote.timestamp
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask <= 0 or ask < bid:
        raise ValueError('Stock bid and ask must be positive, finite, and uncrossed')
    if timestamp is None or timestamp.tzinfo is None:
        raise ValueError('Stock quote timestamp is missing or timezone-naive')
    age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise ValueError('Stock quote is stale or has a future timestamp')
    return (bid + ask) / 2.0


def option_snapshot_metrics(client, symbol: str, max_age_seconds: float = 60.0) -> tuple[float, float, float]:
    from alpaca.data.requests import OptionSnapshotRequest

    contract = str(symbol).strip().upper()
    if not contract or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError('A contract and positive quote freshness limit are required')
    snapshot = client.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=contract))[contract]
    quote = snapshot.latest_quote
    bid = float(quote.bid_price)
    ask = float(quote.ask_price)
    timestamp = quote.timestamp
    implied_volatility = float(snapshot.implied_volatility)
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask <= 0 or ask < bid:
        raise ValueError('Option bid and ask must be positive, finite, and uncrossed')
    if timestamp is None or timestamp.tzinfo is None:
        raise ValueError('Option quote timestamp is missing or timezone-naive')
    age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -5 or age > max_age_seconds:
        raise ValueError('Option quote is stale or has a future timestamp')
    if not math.isfinite(implied_volatility) or implied_volatility <= 0:
        raise ValueError('Option implied volatility is unavailable')
    midpoint = (bid + ask) / 2.0
    return midpoint, (ask - bid) / midpoint, implied_volatility


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
