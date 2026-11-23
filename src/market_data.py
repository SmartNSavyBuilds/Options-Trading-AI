from __future__ import annotations

import math
import os
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, Iterable, Sequence

import pandas as pd
import yfinance as yf

from .quant.contracts import QuoteSnapshot, to_epoch_ms, utc_now_ms


LATEST_OPTION_QUOTES: OrderedDict[str, QuoteSnapshot] = OrderedDict()
_OPTION_QUOTE_CACHE_LIMIT = 512


DEFAULT_TICKERS = [
    'SPY', 'QQQ', 'IWM',
    'AAPL', 'MSFT', 'NVDA', 'META', 'AMZN', 'GOOGL', 'AMD', 'TSLA',
    'JPM', 'GS', 'XOM', 'LLY', 'CRM', 'NFLX',
]


def discovery_tickers(default_tickers: Sequence[str] | None = None) -> list[str]:
    base = [str(ticker).strip().upper() for ticker in (default_tickers or DEFAULT_TICKERS) if str(ticker).strip()]
    configured = str(os.getenv('DISCOVERY_UNIVERSE', '') or '').strip()
    if not configured:
        return base
    override = [token.strip().upper() for token in configured.split(',') if token.strip()]
    return override if override else base


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
    try:
        from .quant.telemetry import telemetry_enabled

        if telemetry_enabled():
            LATEST_OPTION_QUOTES[contract] = QuoteSnapshot(
                symbol=contract,
                bid=bid,
                ask=ask,
                bid_size=_optional_quote_size(quote, 'bid_size'),
                ask_size=_optional_quote_size(quote, 'ask_size'),
                quote_ts_ms=to_epoch_ms(timestamp),
                captured_ts_ms=utc_now_ms(),
            )
            LATEST_OPTION_QUOTES.move_to_end(contract)
            while len(LATEST_OPTION_QUOTES) > _OPTION_QUOTE_CACHE_LIMIT:
                LATEST_OPTION_QUOTES.popitem(last=False)
    except Exception:
        pass
    return midpoint, (ask - bid) / midpoint, implied_volatility


def _optional_quote_size(quote, field: str) -> float | None:
    value = getattr(quote, field, None)
    try:
        size = float(value)
        return size if math.isfinite(size) and size >= 0 else None
    except (TypeError, ValueError):
        return None


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
