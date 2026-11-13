from __future__ import annotations

import math
import os
from bootstrap_path import ensure_project_root_on_path

ensure_project_root_on_path()

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.market_data import DEFAULT_TICKERS, fetch_history
from src.signal_engine import build_signal_report
from src.catalyst_scanner import fetch_news_catalysts
from src.catalyst_events import count_recent_headlines, get_earnings_dates
from src.execution import TradingConfig, _intraday_drift
from src.iv_history import safety_database_path
from src.market_data import stock_quote_midpoint


OUTPUT_DIR = Path(__file__).resolve().parent / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)


def build_discovery_feed(signals: pd.DataFrame, catalysts: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    catalyst_map: dict[str, dict[str, object]] = {}

    if not catalysts.empty:
        for ticker, group in catalysts.groupby('ticker'):
            catalyst_map[str(ticker)] = {
                'headline': str(group.iloc[0].get('headline', 'No recent headline')),
                'sentiment_score': int(group['sentiment_score'].sum()),
                'catalyst_type': str(group.iloc[0].get('catalyst_type', 'neutral')),
            }

    for _, row in signals.iterrows():
        ticker = row.get('ticker', 'n/a')
        bias = row.get('bias', 'neutral')
        score = int(row.get('signal_score', 0))
        source = str(row.get('opportunity_source', 'price_action_only'))
        summary = str(row.get('advisor_summary', 'No summary available.'))
        catalyst = catalyst_map.get(str(ticker), {'headline': 'No recent catalyst headline pulled', 'sentiment_score': 0, 'catalyst_type': 'neutral'})

        if bias == 'bullish':
            event_watch = 'watch earnings beats, positive guidance, analyst upgrades, and strong sector rotation'
        elif bias == 'bearish':
            event_watch = 'watch earnings misses, downgrades, broken support, and negative macro news'
        else:
            event_watch = 'watch for catalyst confirmation before acting'

        rows.append({
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'ticker': ticker,
            'bias': bias,
            'signal_score': score,
            'discovery_source': source,
            'event_watch': event_watch,
            'recent_headline': catalyst['headline'],
            'headline_sentiment_score': catalyst['sentiment_score'],
            'catalyst_type': catalyst['catalyst_type'],
            'advisor_summary': summary,
        })

    feed = pd.DataFrame(rows)
    if not feed.empty:
        feed = feed.sort_values(['signal_score', 'ticker'], ascending=[False, True])
    return feed


def build_catalyst_inputs(signals: pd.DataFrame, config: TradingConfig) -> pd.DataFrame:
    from zoneinfo import ZoneInfo

    from alpaca.data.historical import NewsClient, StockHistoricalDataClient

    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo('America/New_York')).date()
    rows = []
    for _, signal in signals.iterrows():
        ticker = str(signal.get('ticker', '')).strip().upper()
        result = {'ticker': ticker, 'catalyst_checked_at_utc': now.isoformat(),
                  'catalyst_status': 'UNKNOWN', 'catalyst_reason': 'Safety data unavailable',
                  'earnings_dates': '', 'headline_count_60m': None}
        try:
            if not config.api_key or not config.secret_key:
                raise ValueError('Alpaca credentials missing')
            blackout_days = int(os.getenv('EARNINGS_BLACKOUT_DAYS', '7'))
            news_threshold = int(os.getenv('BREAKING_NEWS_COUNT_THRESHOLD', '3'))
            news_move = float(os.getenv('BREAKING_NEWS_MOVE_PCT', '0.04'))
            if news_threshold <= 0 or not math.isfinite(news_move) or news_move < 0:
                raise ValueError('Invalid news thresholds')
            quote_max_age = float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60'))
            database = safety_database_path(OUTPUT_DIR.parent)
            midpoint = stock_quote_midpoint(StockHistoricalDataClient(config.api_key, config.secret_key), ticker, quote_max_age)
            drift = _intraday_drift(signal.get('last_close'), midpoint)
            if drift is None:
                raise ValueError('Signal reference unavailable')
            earnings = get_earnings_dates(database, ticker, os.getenv('FINNHUB_API_KEY', ''), today, blackout_days)
            count = count_recent_headlines(NewsClient(config.api_key, config.secret_key), ticker, now, news_threshold)
            upcoming = [event for event in earnings if 0 <= (event - today).days <= blackout_days]
            result.update({'earnings_dates': ','.join(event.isoformat() for event in earnings),
                           'headline_count_60m': count, 'catalyst_status': 'CLEAR', 'catalyst_reason': ''})
            if upcoming or (count >= news_threshold and abs(drift) >= news_move):
                result['catalyst_status'] = 'MANUAL_REVIEW_REQUIRED'
                result['catalyst_reason'] = 'Earnings blackout' if upcoming else f'{count} headlines; drift {drift * 100:.2f}%'
        except Exception as exc:
            result['catalyst_reason'] = f'Safety data unavailable ({type(exc).__name__})'
        rows.append(result)
    return pd.DataFrame(rows, columns=['ticker', 'catalyst_checked_at_utc', 'catalyst_status',
                                       'catalyst_reason', 'earnings_dates', 'headline_count_60m'])


def main() -> None:
    market_data = fetch_history(DEFAULT_TICKERS, period='6mo', interval='1d')
    signals = build_signal_report(market_data)
    catalysts = fetch_news_catalysts(DEFAULT_TICKERS)
    feed = build_discovery_feed(signals, catalysts)
    if not feed.empty:
        feed = feed.merge(build_catalyst_inputs(signals, TradingConfig.from_env()), on='ticker', how='left')

    out_file = OUTPUT_DIR / 'opportunity_discovery.csv'
    catalyst_file = OUTPUT_DIR / 'catalyst_news.csv'
    feed.to_csv(out_file, index=False)
    catalysts.to_csv(catalyst_file, index=False)

    print('Opportunity discovery feed generated:')
    print(out_file)
    print('Catalyst news feed generated:')
    print(catalyst_file)
    print()
    print(feed.to_string(index=False))


if __name__ == '__main__':
    main()
