from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.quant.chain_store import upsert_chain_records
from src.quant.contracts import ChainRecord, ExecutionEvent, QuoteSnapshot, to_epoch_ms
from src.quant.db import connect, init_schema
from src.quant.ingest import ingest_history
from src.quant.iv_population import (
    IV_METHOD,
    calculate_iv_percentile,
    derive_atm_30d_observations,
    historical_percentile,
    populate_iv_percentiles,
)
from src.quant.providers.base import ChainPage
from src.quant.telemetry import record_broker_fills, record_event, record_spread_event


def test_schema_and_vendor_neutral_chain_upsert(tmp_path: Path) -> None:
    database = tmp_path / 'quant.sqlite3'
    init_schema(database)
    init_schema(database)
    record = ChainRecord.from_mapping({
        'symbol': 'NVDA261218C00100000', 'timestamp': '2026-11-16T15:30:00.123Z',
        'o': 1.0, 'h': 1.2, 'l': 0.9, 'c': 1.1, 'v': 50, 'bid': 1.05, 'ask': 1.15,
        'impliedVolatility': 0.42, 'provider': 'fixture', 'interval': '1m',
        'vendor_extra': {'field': 'preserved'},
    })
    with connect(database) as connection:
        assert upsert_chain_records(connection, [record]) == 1
        assert upsert_chain_records(connection, [record]) == 1
        row = connection.execute(
            'SELECT underlying, expiry, strike, right, ts_ms, volume, iv, mid, spread, spread_pct, extra_json '
            'FROM option_chain_snapshots'
        ).fetchone()
        assert row['underlying'] == 'NVDA'
        assert row['right'] == 'C'
        assert row['volume'] == 50
        assert row['iv'] == 0.42
        assert row['mid'] == pytest.approx(1.1)
        assert row['spread'] == pytest.approx(0.1)
        assert row['ts_ms'] == 1794843000123
        assert json.loads(row['extra_json'])['vendor_extra'] == {'field': 'preserved'}
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 3
        assert connection.execute('SELECT COUNT(*) FROM option_chain_snapshots').fetchone()[0] == 1


def test_schema_migrates_existing_v2_database_to_v3(tmp_path: Path) -> None:
    database = tmp_path / 'quant.sqlite3'
    init_schema(database)
    with connect(database) as connection:
        connection.execute('DROP TABLE iv_percentiles')
        connection.execute('PRAGMA user_version = 2')
    init_schema(database)
    with connect(database) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 3
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='iv_percentiles'"
        ).fetchone() is not None


def test_chain_contract_flags_bad_quotes_and_accepts_vendor_greeks() -> None:
    record = ChainRecord.from_mapping({
        'provider': 'massive', 'interval': 'eod', 'timestamp': 1794843000,
        'contract_symbol': 'AAPL261218P00150000', 'bid_price': 1.2, 'ask_price': 1.0,
        'delta': -0.3, 'gamma': 0.04, 'theta': -0.02, 'vega': 0.1, 'rho': -0.01,
        'iv': 5.5,
    })
    assert record.underlying == 'AAPL'
    assert record.right == 'P'
    assert record.quality_flags == ('crossed_quote', 'iv_out_of_range')
    assert record.delta == -0.3
    assert record.ts_ms == 1794843000000


def test_chain_contract_flattens_nested_vendor_sections() -> None:
    record = ChainRecord.from_mapping({
        'provider': 'massive', 'interval': 'eod', 'timestamp': '2026-11-16T21:00:00Z',
        'contract_symbol': 'O:AAPL261218P00150000',
        'day': {'open': 1.0, 'high': 1.4, 'low': 0.9, 'close': 1.2, 'volume': 75, 'vwap': 1.15},
        'quote': {'bid': 1.1, 'ask': 1.3, 'timestamp': '2026-11-16T20:59:59.500Z'},
        'greeks': {'delta': -0.25, 'gamma': 0.03, 'theta': -0.02, 'vega': 0.08, 'rho': -0.01},
        'impliedVolatility': 0.32,
    })
    assert (record.open, record.high, record.low, record.close, record.volume) == (1, 1.4, 0.9, 1.2, 75)
    assert (record.bid, record.ask, record.iv, record.delta) == (1.1, 1.3, 0.32, -0.25)
    assert record.underlying == 'AAPL'
    assert record.right == 'P'
    assert record.quote_ts_ms == 1794862799500


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('2026-11-16T15:30:00.123Z', 1794843000123),
        (1794843000, 1794843000000),
        (1794843000123, 1794843000123),
        (1794843000123000, 1794843000123),
        (1794843000123000000, 1794843000123),
    ],
)
def test_to_epoch_ms_normalizes_timestamp_precision(value, expected: int) -> None:
    assert to_epoch_ms(value) == expected


def test_to_epoch_ms_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match='Naive'):
        to_epoch_ms(datetime(2026, 11, 16, 15, 30))


def test_telemetry_disabled_does_not_create_database(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / 'quant.sqlite3'
    monkeypatch.setenv('QUANT_TELEMETRY_ENABLED', 'false')
    event = ExecutionEvent(client_order_id='paper-test', event='signal', event_ts_ms=1)
    assert not record_event(event, database)
    assert not record_spread_event('signal', {'client_order_id': 'paper-test'}, database=database)
    assert not database.exists()


def test_record_spread_event_stores_quote_snapshots_and_isolates_errors(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / 'quant.sqlite3'
    monkeypatch.setenv('QUANT_TELEMETRY_ENABLED', 'true')
    monkeypatch.setenv('QUANT_DB', str(database))
    from src.market_data import LATEST_OPTION_QUOTES

    LATEST_OPTION_QUOTES.clear()
    now_ms = to_epoch_ms(datetime.now(timezone.utc))
    LATEST_OPTION_QUOTES['NVDA261218C00100000'] = QuoteSnapshot(
        'NVDA261218C00100000', 1.0, 1.2, 10, 12, now_ms, now_ms,
    )
    LATEST_OPTION_QUOTES['NVDA261218C00105000'] = QuoteSnapshot(
        'NVDA261218C00105000', 0.4, 0.6, 8, 9, now_ms, now_ms,
    )
    order = {
        'client_order_id': 'paper-spread-NVDA-test', 'long_leg_symbol': 'NVDA261218C00100000',
        'short_leg_symbol': 'NVDA261218C00105000', 'proxy_qty': 1, 'proxy_side': 'buy',
        'long_leg_intent': 'buy_to_open', 'short_leg_intent': 'sell_to_open',
    }
    assert record_spread_event('signal', order, event_ts_ms=now_ms, database=database)
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            'SELECT combo_bid, combo_ask, combo_mid, combo_spread, legs_json '
            'FROM execution_events WHERE event="signal"'
        ).fetchone()
    finally:
        connection.close()
    assert row[:4] == pytest.approx((0.4, 0.8, 0.6, 0.4))
    assert len(json.loads(row[4])) == 2
    assert record_spread_event('invalid', order, database=database) is False


def test_fill_telemetry_skips_stale_quote_lookup_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / 'quant.sqlite3'
    monkeypatch.setenv('QUANT_TELEMETRY_ENABLED', 'true')
    monkeypatch.setenv('QUANT_FILL_QUOTE_MAX_LAG_MS', '120000')
    now_ms = to_epoch_ms(datetime.now(timezone.utc))
    calls = []
    stale_order = {
        'id': 'broker-1', 'client_order_id': 'paper-spread-NVDA-stale', 'order_class': 'mleg',
        'status': 'filled', 'filled_at': now_ms - 180000, 'filled_qty': '1',
        'filled_avg_price': '0.60', 'qty': '1', 'side': 'buy',
        'legs': [{'symbol': 'NVDA261218C00100000'}, {'symbol': 'NVDA261218C00105000'}],
    }
    assert record_broker_fills([stale_order], type('Config', (), {'trading_mode': 'paper'})(),
                               lambda symbol: calls.append(symbol), database) == 1
    assert calls == []
    assert record_broker_fills([stale_order], type('Config', (), {'trading_mode': 'paper'})(),
                               lambda symbol: calls.append(symbol), database) == 0
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            'SELECT combo_mid, quality_flags FROM execution_events WHERE event="fill"'
        ).fetchone()
        assert connection.execute('SELECT COUNT(*) FROM execution_events').fetchone()[0] == 1
    finally:
        connection.close()
    assert row[0] is None
    assert 'fill_quote_lookup_skipped_stale_fill' in json.loads(row[1])


def test_fill_telemetry_records_fresh_quotes_and_signal_relative_slippage(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / 'quant.sqlite3'
    monkeypatch.setenv('QUANT_TELEMETRY_ENABLED', 'true')
    monkeypatch.setenv('QUANT_FILL_QUOTE_MAX_LAG_MS', '120000')
    from src.market_data import LATEST_OPTION_QUOTES

    LATEST_OPTION_QUOTES.clear()
    now_ms = to_epoch_ms(datetime.now(timezone.utc))
    symbols = ('NVDA261218C00100000', 'NVDA261218C00105000')
    quote_mids = {symbols[0]: (1.1, 1.3), symbols[1]: (0.4, 0.6)}

    def quote_fetcher(symbol: str) -> QuoteSnapshot:
        bid, ask = quote_mids[symbol]
        return QuoteSnapshot(symbol, bid, ask, 10, 10, now_ms, now_ms)

    signal_order = {
        'client_order_id': 'paper-spread-NVDA-fill-test',
        'long_leg_symbol': symbols[0], 'short_leg_symbol': symbols[1],
        'long_leg_intent': 'buy_to_open', 'short_leg_intent': 'sell_to_open',
        'proxy_side': 'buy', 'proxy_qty': 1,
    }
    assert record_spread_event(
        'signal', signal_order, event_ts_ms=now_ms - 1000, quote_fetcher=quote_fetcher,
        database=database,
    )
    order = {
        'id': 'broker-fill-1', 'client_order_id': signal_order['client_order_id'],
        'order_class': 'mleg', 'status': 'filled', 'filled_at': now_ms,
        'filled_qty': '1', 'filled_avg_price': '0.62', 'qty': '1', 'side': 'buy',
        'legs': [{'symbol': symbols[0], 'side': 'buy'}, {'symbol': symbols[1], 'side': 'sell'}],
    }
    assert record_broker_fills(
        [order], type('Config', (), {'trading_mode': 'paper'})(), quote_fetcher, database,
    ) == 1
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            'SELECT combo_bid, combo_ask, combo_mid, quote_lag_ms, slippage_bps_vs_signal_mid '
            'FROM execution_events WHERE event="fill"'
        ).fetchone()
    finally:
        connection.close()
    assert row[:3] == pytest.approx((0.5, 0.9, 0.7))
    assert row[3] <= 120000
    assert row[4] == pytest.approx((0.62 - 0.7) / 0.7 * 10000)


def test_history_ingestion_resumes_and_skips_completed_windows(tmp_path: Path) -> None:
    class FakeProvider:
        name = 'fixture'

        def __init__(self) -> None:
            self.cursors = []

        def fetch_chain_page(self, underlying, start_date, end_date, interval, cursor):
            self.cursors.append(cursor)
            if cursor is None:
                return ChainPage([{
                    'symbol': f'{underlying}261218C00100000', 'timestamp': '2026-11-02T20:00:00Z',
                    'bid': 1.0, 'ask': 1.1, 'iv': 0.25,
                }], 'page-2')
            assert cursor == 'page-2'
            return ChainPage([{
                'symbol': f'{underlying}261218C00105000', 'timestamp': '2026-11-03T20:00:00Z',
                'bid': 0.8, 'ask': 0.9, 'iv': 0.27,
            }], None)

    database = tmp_path / 'quant.sqlite3'
    provider = FakeProvider()
    first = ingest_history(
        provider, ['NVDA'], datetime(2026, 11, 2).date(), datetime(2026, 11, 4).date(),
        database=database, max_pages_per_symbol=1,
    )
    assert first.pages_ingested == 1
    assert first.records_ingested == 1
    assert first.completed_symbols == ()

    resumed = ingest_history(
        provider, ['NVDA'], datetime(2026, 11, 2).date(), datetime(2026, 11, 4).date(),
        database=database,
    )
    assert provider.cursors == [None, 'page-2']
    assert resumed.completed_symbols == ('NVDA',)
    assert resumed.records_ingested == 1

    skipped = ingest_history(
        provider, ['NVDA'], datetime(2026, 11, 2).date(), datetime(2026, 11, 4).date(),
        database=database,
    )
    assert skipped.skipped_symbols == ('NVDA',)
    assert provider.cursors == [None, 'page-2']
    with connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM option_chain_snapshots').fetchone()[0] == 2
        checkpoint = connection.execute(
            'SELECT complete, pages_ingested, records_ingested FROM ingestion_checkpoints'
        ).fetchone()
        assert tuple(checkpoint) == (1, 2, 2)


def test_derive_30d_iv_interpolates_total_variance_and_does_not_extrapolate(tmp_path: Path) -> None:
    database = tmp_path / 'quant.sqlite3'
    init_schema(database)

    def add_chain_row(observed_date: date, dte: int, iv: float) -> None:
        expiry = observed_date + timedelta(days=dte)
        contract = f'SPY{expiry:%y%m%d}C00100000'
        record = ChainRecord.from_mapping({
            'provider': 'fixture', 'interval': 'eod', 'timestamp': f'{observed_date.isoformat()}T20:00:00Z',
            'symbol': contract, 'underlying': 'SPY', 'expiry': expiry.isoformat(), 'strike': 100,
            'right': 'C', 'underlying_px': 100, 'bid': 1.0, 'ask': 1.02, 'iv': iv,
        })
        with connect(database) as connection:
            upsert_chain_records(connection, [record])

    observed_date = date(2026, 1, 2)
    add_chain_row(observed_date, 20, 0.2)
    add_chain_row(observed_date, 40, 0.4)
    no_bracket_date = observed_date + timedelta(days=1)
    add_chain_row(no_bracket_date, 20, 0.25)

    assert derive_atm_30d_observations(database, source='fixture') == 1
    with connect(database) as connection:
        row = connection.execute(
            'SELECT iv, method, extra_json FROM iv_observations'
        ).fetchone()
    assert row['iv'] == pytest.approx(0.12 ** 0.5)
    assert row['method'] == IV_METHOD
    assert len(json.loads(row['extra_json'])['expiry_points']) == 2
    assert row['iv'] > 0.2 and row['iv'] < 0.4


def test_derive_30d_iv_filters_extreme_near_atm_iv_outlier(tmp_path: Path) -> None:
    database = tmp_path / 'quant.sqlite3'
    init_schema(database)
    observed_date = date(2026, 1, 2)
    rows = ((20, ((99.5, 0.2), (100.5, 0.21), (99.0, 0.19), (100.0, 2.0))),
            (40, ((100.0, 0.4), (99.5, 0.41), (100.5, 0.39))))
    with connect(database) as connection:
        for dte, quotes in rows:
            expiry = observed_date + timedelta(days=dte)
            for strike, iv in quotes:
                contract = f'SPY{expiry:%y%m%d}C{int(strike * 1000):08d}'
                record = ChainRecord.from_mapping({
                    'provider': 'fixture', 'interval': 'eod',
                    'timestamp': f'{observed_date.isoformat()}T20:00:00Z',
                    'symbol': contract, 'underlying': 'SPY', 'expiry': expiry.isoformat(),
                    'strike': strike, 'right': 'C', 'underlying_px': 100,
                    'bid': 1.0, 'ask': 1.02, 'iv': iv,
                })
                upsert_chain_records(connection, [record])

    assert derive_atm_30d_observations(database, source='fixture') == 1
    with connect(database) as connection:
        row = connection.execute('SELECT iv, extra_json FROM iv_observations').fetchone()
    provenance = json.loads(row['extra_json'])
    low_expiry = min(provenance['expiry_points'], key=lambda point: point['dte'])
    expected = math.sqrt((0.205 ** 2 * 20 + 0.4 ** 2 * 40) / 60)
    assert low_expiry['iv'] == pytest.approx(0.205)
    assert row['iv'] == pytest.approx(expected)


def test_iv_percentiles_use_only_prior_observations_and_are_repeatable(tmp_path: Path) -> None:
    database = tmp_path / 'quant.sqlite3'
    init_schema(database)
    values = (0.1, 0.2, 0.3, 0.4, 0.15)
    with connect(database) as connection:
        connection.executemany(
            '''INSERT INTO iv_observations (
                source, symbol, obs_ts_ms, tenor_days, method, iv, ingested_ts_ms
            ) VALUES ('fixture', 'SPY', ?, 30, ?, ?, 1)''',
            [(index, IV_METHOD, value) for index, value in enumerate(values, start=1)],
        )

    percentile, count = calculate_iv_percentile(
        database, 'fixture', 'spy', 0.4, 4, window_size=3, min_observations=3,
    )
    assert percentile == 100
    assert count == 3
    assert historical_percentile([0.1, 0.2, 0.3], 0.3, window_size=3, min_observations=3) == 100
    assert populate_iv_percentiles(database, source='fixture', window_size=3, min_observations=3) == 2
    with connect(database) as connection:
        rows = connection.execute(
            'SELECT obs_ts_ms, observations_used, percentile FROM iv_percentiles ORDER BY obs_ts_ms'
        ).fetchall()
        assert [tuple(row) for row in rows] == [(4, 3, 100.0), (5, 3, 0.0)]