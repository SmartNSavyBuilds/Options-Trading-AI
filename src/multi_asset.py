from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .market_data import fetch_history
from .signal_engine import build_signal_report


CRYPTO_TICKERS = {
    'BTC-USD': 'BTC',
    'ETH-USD': 'ETH',
    'SOL-USD': 'SOL',
    'DOGE-USD': 'DOGE',
    'XRP-USD': 'XRP',
}

CRYPTO_WATCHLIST_COLUMNS = [
    'generated_at_utc',
    'ticker',
    'source_symbol',
    'asset_class',
    'bias',
    'signal_score',
    'annualized_volatility',
    'conviction_label',
    'crypto_action',
    'advisor_summary',
]

MARKET_REGIME_COLUMNS = [
    'generated_at_utc',
    'bullish_ratio',
    'bearish_ratio',
    'avg_signal_score',
    'avg_volatility',
    'market_regime',
    'regime_note',
]


def normalize_crypto_signals(report: pd.DataFrame) -> pd.DataFrame:
    if report.empty:
        return pd.DataFrame(columns=list(report.columns) + ['source_symbol', 'asset_class'])

    normalized = report.copy()
    normalized['source_symbol'] = normalized['ticker'].astype(str)
    normalized['ticker'] = normalized['ticker'].astype(str).map(lambda value: CRYPTO_TICKERS.get(value, value.replace('-USD', '')))
    normalized['asset_class'] = 'crypto'
    return normalized


def build_crypto_watchlist(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame(columns=CRYPTO_WATCHLIST_COLUMNS)

    working = signals.copy()
    if 'asset_class' not in working.columns:
        working['asset_class'] = working['ticker'].astype(str).map(lambda value: 'crypto' if value in CRYPTO_TICKERS.values() or value in CRYPTO_TICKERS.keys() else 'equity')
    if 'source_symbol' not in working.columns:
        working['source_symbol'] = working['ticker'].astype(str)

    working = working[working['asset_class'].astype(str).str.lower().eq('crypto')].copy()
    if working.empty:
        return pd.DataFrame(columns=CRYPTO_WATCHLIST_COLUMNS)

    def _conviction_label(score: int) -> str:
        absolute = abs(int(score or 0))
        if absolute >= 7:
            return 'high'
        if absolute >= 4:
            return 'medium'
        return 'developing'

    def _crypto_action(row: pd.Series) -> str:
        bias = str(row.get('bias', 'neutral')).lower()
        score = int(row.get('signal_score', 0) or 0)
        if bias == 'bullish' and score >= 5:
            return 'paper_long_watch'
        if bias == 'bearish' and score <= -5:
            return 'paper_short_or_hedge_watch'
        return 'monitor_only'

    working['generated_at_utc'] = datetime.now(timezone.utc).isoformat()
    working['conviction_label'] = working['signal_score'].map(_conviction_label)
    working['crypto_action'] = working.apply(_crypto_action, axis=1)
    working['annualized_volatility'] = pd.to_numeric(working.get('annualized_volatility', 0.0), errors='coerce').fillna(0.0).round(3)
    working['advisor_summary'] = working.get('advisor_summary', pd.Series('No crypto summary available.', index=working.index)).fillna('No crypto summary available.')
    watchlist = working[CRYPTO_WATCHLIST_COLUMNS].sort_values(['signal_score', 'ticker'], ascending=[False, True]).reset_index(drop=True)
    return watchlist


def build_market_regime_summary(signals: pd.DataFrame) -> pd.DataFrame:
    generated_at_utc = datetime.now(timezone.utc).isoformat()
    if signals.empty:
        return pd.DataFrame(
            [{
                'generated_at_utc': generated_at_utc,
                'bullish_ratio': 0.0,
                'bearish_ratio': 0.0,
                'avg_signal_score': 0.0,
                'avg_volatility': 0.0,
                'market_regime': 'unknown',
                'regime_note': 'No signal data available yet.',
            }],
            columns=MARKET_REGIME_COLUMNS,
        )

    working = signals.copy()
    bullish_ratio = round(float(working['bias'].astype(str).str.lower().eq('bullish').mean()), 2)
    bearish_ratio = round(float(working['bias'].astype(str).str.lower().eq('bearish').mean()), 2)
    avg_signal_score = round(float(pd.to_numeric(working.get('signal_score', 0.0), errors='coerce').fillna(0.0).mean()), 2)
    avg_volatility = round(float(pd.to_numeric(working.get('annualized_volatility', 0.0), errors='coerce').fillna(0.0).mean()), 3)

    if bullish_ratio >= 0.6 and avg_signal_score >= 2:
        market_regime = 'risk_on'
        regime_note = 'Momentum and breadth are supportive; focus on disciplined long exposure.'
    elif bearish_ratio >= 0.45 and avg_signal_score <= -1:
        market_regime = 'risk_off'
        regime_note = 'Tape is defensive; tighten risk and favor hedges or smaller position sizes.'
    else:
        market_regime = 'mixed'
        regime_note = 'Breadth is mixed; emphasize selectivity and fast risk review.'

    return pd.DataFrame(
        [{
            'generated_at_utc': generated_at_utc,
            'bullish_ratio': bullish_ratio,
            'bearish_ratio': bearish_ratio,
            'avg_signal_score': avg_signal_score,
            'avg_volatility': avg_volatility,
            'market_regime': market_regime,
            'regime_note': regime_note,
        }],
        columns=MARKET_REGIME_COLUMNS,
    )


def generate_multi_asset_outputs(project_dir: Path) -> tuple[Path, Path, pd.DataFrame, pd.DataFrame]:
    output_dir = project_dir / 'outputs'
    output_dir.mkdir(exist_ok=True)

    market_data = fetch_history(CRYPTO_TICKERS.keys(), period='6mo', interval='1d')
    raw_report = build_signal_report(market_data)
    crypto_signals = normalize_crypto_signals(raw_report)
    watchlist = build_crypto_watchlist(crypto_signals)
    regime = build_market_regime_summary(crypto_signals)

    watchlist_path = output_dir / 'crypto_watchlist.csv'
    regime_path = output_dir / 'market_regime.csv'
    watchlist.to_csv(watchlist_path, index=False)
    regime.to_csv(regime_path, index=False)
    return watchlist_path, regime_path, watchlist, regime
