from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.market_data import DEFAULT_TICKERS, fetch_history
from src.signal_engine import build_signal_report
from src.catalyst_scanner import fetch_news_catalysts


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


def main() -> None:
    market_data = fetch_history(DEFAULT_TICKERS, period='6mo', interval='1d')
    signals = build_signal_report(market_data)
    catalysts = fetch_news_catalysts(DEFAULT_TICKERS)
    feed = build_discovery_feed(signals, catalysts)

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
