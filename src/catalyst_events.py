from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests


def count_recent_headlines(client, symbol: str, now: datetime, threshold: int = 3) -> int:
    from alpaca.data.requests import NewsRequest

    ticker = str(symbol).strip().upper()
    if not ticker or threshold <= 0 or now.tzinfo is None:
        raise ValueError('Ticker, aware timestamp, and positive news threshold required')
    end = now.astimezone(timezone.utc)
    start = end - timedelta(minutes=60)
    try:
        response = client.get_news(NewsRequest(symbols=ticker, start=start, end=end, limit=max(50, threshold)))
        articles = response.data['news']
        if not isinstance(articles, list):
            raise ValueError('Invalid news response')
        matching = set()
        for article in articles:
            created = article.created_at
            if created is None or created.tzinfo is None or article.symbols is None or article.id is None:
                raise ValueError('Invalid news article metadata')
            if start <= created.astimezone(timezone.utc) <= end and ticker in {str(item).upper() for item in article.symbols}:
                matching.add(str(article.id))
        if response.next_page_token and len(matching) < threshold:
            raise ValueError('Incomplete news response')
        return len(matching)
    except Exception as exc:
        raise RuntimeError('Alpaca news lookup failed') from exc


def get_earnings_dates(database: Path, symbol: str, api_key: str, as_of: date, blackout_days: int = 7) -> list[date]:
    ticker = str(symbol).strip().upper()
    if not ticker or blackout_days < 0 or blackout_days > 365:
        raise ValueError('Invalid earnings lookup parameters')
    if not api_key or not api_key.strip():
        raise RuntimeError('Finnhub earnings credentials unavailable')
    database.parent.mkdir(parents=True, exist_ok=True)
    end_date = as_of + timedelta(days=blackout_days)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            'CREATE TABLE IF NOT EXISTS earnings_calendar '
            '(symbol TEXT NOT NULL, queried_date TEXT NOT NULL, end_date TEXT NOT NULL, '
            'dates_json TEXT NOT NULL, PRIMARY KEY(symbol, queried_date, end_date))'
        )
        cached = connection.execute(
            'SELECT dates_json FROM earnings_calendar WHERE symbol=? AND queried_date=? AND end_date=?',
            (ticker, as_of.isoformat(), end_date.isoformat()),
        ).fetchone()
        if cached is not None:
            return [date.fromisoformat(item) for item in json.loads(cached[0])]
        try:
            response = requests.get(
                'https://finnhub.io/api/v1/calendar/earnings',
                params={'symbol': ticker, 'from': as_of.isoformat(), 'to': end_date.isoformat()},
                headers={'X-Finnhub-Token': api_key}, timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            entries = payload['earningsCalendar']
            if not isinstance(entries, list):
                raise ValueError('Invalid earnings calendar response')
            dates = sorted({
                date.fromisoformat(item['date']) for item in entries
                if str(item['symbol']).upper() == ticker
            })
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError('Finnhub earnings lookup failed') from exc
        connection.execute(
            'INSERT INTO earnings_calendar(symbol, queried_date, end_date, dates_json) VALUES (?, ?, ?, ?)',
            (ticker, as_of.isoformat(), end_date.isoformat(), json.dumps([item.isoformat() for item in dates])),
        )
        return dates