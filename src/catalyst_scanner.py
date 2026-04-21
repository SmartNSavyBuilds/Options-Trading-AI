from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
import yfinance as yf


KEYWORD_SCORES = {
    'upgrade': 1,
    'beat': 1,
    'growth': 1,
    'partnership': 1,
    'expansion': 1,
    'downgrade': -1,
    'miss': -1,
    'lawsuit': -1,
    'investigation': -1,
    'layoff': -1,
    'warning': -1,
}


def fetch_news_catalysts(tickers: Iterable[str], limit_per_ticker: int = 5) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    columns = [
        'fetched_at_utc',
        'ticker',
        'headline',
        'sentiment_score',
        'catalyst_type',
        'published_time',
        'link',
    ]

    for ticker in tickers:
        try:
            news_items = yf.Ticker(ticker).news or []
        except Exception:
            news_items = []

        for item in news_items[:limit_per_ticker]:
            title = str(item.get('title', '')).strip()
            published = item.get('providerPublishTime')
            link = str(item.get('link', '')).strip()
            if not title and not link:
                continue
            lowered = title.lower()
            sentiment_score = sum(score for keyword, score in KEYWORD_SCORES.items() if keyword in lowered)
            catalyst_type = 'positive' if sentiment_score > 0 else 'negative' if sentiment_score < 0 else 'neutral'

            rows.append({
                'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
                'ticker': ticker,
                'headline': title,
                'sentiment_score': sentiment_score,
                'catalyst_type': catalyst_type,
                'published_time': published,
                'link': link,
            })

    result = pd.DataFrame(rows, columns=columns)
    if not result.empty:
        result = result.sort_values(['sentiment_score', 'ticker'], ascending=[True, True])
    return result
