from __future__ import annotations

import os
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

import market_monitor as monitor
from paper_trade import build_paper_trade_queue
from src.congressional_disclosures import import_raw_trade_data, build_public_interest_watchlist, build_recent_large_trades
from src.execution import (
    TradingConfig,
    choose_best_option_contract,
    prepare_equity_execution_preview,
    prepare_exit_execution_preview,
    prepare_option_execution_preview,
)
from evaluate_exit_rules import apply_automatic_exit_guardrails
from src.exit_manager import build_exit_recommendations
from src.multi_asset import build_crypto_watchlist, build_market_regime_summary
from src.options_selector import build_options_candidates
from src.performance_journal import add_execution_status_to_candidates, build_execution_quality_report, build_open_trade_timeline, build_performance_journal, build_priority_alerts, build_strategy_attribution, humanize_display_text
from src.risk_guardrails import build_exposure_summary, build_risk_overview, build_stress_test_table
from src.signal_engine import build_signal_report


def _make_market_frame(prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            'Close': prices,
            'Volume': [1_000_000 + i * 5_000 for i in range(len(prices))],
        }
    )


def test_iv_history_rank_uses_historical_rows(tmp_path: Path) -> None:
    from src.iv_history import calculate_iv_rank, record_iv

    database = tmp_path / 'iv.db'
    today = datetime.now(timezone.utc).date()
    for index in range(20):
        record_iv(database, 'NVDA', today - timedelta(days=index + 1), 0.2 + index * 0.01)
    record_iv(database, 'NVDA', today, 0.9)
    rank, count = calculate_iv_rank(database, 'nvda', 0.295, today)
    assert count == 20
    assert abs(rank - 50.0) < 1e-8
    assert calculate_iv_rank(database, 'MISSING', 0.3, today) == (None, 0)


def test_realized_volatility_proxy_requires_sufficient_variation() -> None:
    from src.iv_history import realized_volatility_proxy_rank

    assert realized_volatility_proxy_rank(pd.Series([100.0] * 30)) is None
    prices = pd.Series([100 + index * 0.1 + (index % 7) ** 2 * 0.15 for index in range(100)])
    rank = realized_volatility_proxy_rank(prices)
    assert rank is not None and 0 <= rank <= 100


def test_eod_iv_records_snapshot_without_orders(monkeypatch, tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    import record_eod_iv as entry

    monkeypatch.setattr(entry, 'StockHistoricalDataClient', lambda *args: object())
    monkeypatch.setattr(entry, 'OptionHistoricalDataClient', lambda *args: object())
    monkeypatch.setattr(entry, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(entry, 'fetch_option_contracts', lambda *args, **kwargs: pd.DataFrame([{'symbol': 'NVDA'}]))
    monkeypatch.setattr(entry, 'choose_best_option_contract', lambda *args, **kwargs: {'symbol': 'NVDA', 'strike_price': 100.0})
    monkeypatch.setattr(entry, 'option_snapshot_metrics', lambda *args: (1.0, 0.02, 0.5))
    database = tmp_path / 'iv.sqlite'
    assert entry.record_snapshots(TradingConfig(api_key='fake', secret_key='fake'), ['NVDA'], database) == []
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('SELECT symbol, atm_iv_30d FROM iv_history').fetchone() == ('NVDA', 0.5)


def test_stock_quote_midpoint_rejects_stale_and_crossed_quotes() -> None:
    from types import SimpleNamespace

    import pytest

    from src.market_data import stock_quote_midpoint

    class FakeClient:
        def __init__(self, quote):
            self.quote = quote

        def get_stock_latest_quote(self, request):
            return {'NVDA': self.quote}

    quote = SimpleNamespace(bid_price=99.0, ask_price=101.0, timestamp=datetime.now(timezone.utc))
    assert stock_quote_midpoint(FakeClient(quote), 'nvda') == 100.0
    quote.timestamp -= timedelta(minutes=5)
    with pytest.raises(ValueError, match='stale'):
        stock_quote_midpoint(FakeClient(quote), 'NVDA')
    quote.timestamp = datetime.now(timezone.utc)
    quote.ask_price = 98.0
    with pytest.raises(ValueError, match='uncrossed'):
        stock_quote_midpoint(FakeClient(quote), 'NVDA')


def test_candidate_strikes_use_live_midpoint_and_reject_drift(monkeypatch) -> None:
    from types import SimpleNamespace

    import src.options_selector as selector

    signals = pd.DataFrame([{'ticker': 'NVDA', 'bias': 'bullish', 'suggested_structure': 'Long call',
                             'last_close': 100.0, 'signal_score': 5, 'annualized_volatility': 0.2}])
    monkeypatch.setattr(selector.yf, 'Ticker', lambda ticker: SimpleNamespace(options=[]))
    monkeypatch.setattr(selector, 'stock_quote_midpoint', lambda *args: 102.0)
    row = selector.build_options_candidates(signals, pd.DataFrame(), quote_client=object()).iloc[0]
    assert row['options_setup'] == 'Buy 102C'
    assert row['signal_close'] == 100.0
    monkeypatch.setattr(selector, 'stock_quote_midpoint', lambda *args: 106.0)
    row = selector.build_options_candidates(signals, pd.DataFrame(), quote_client=object()).iloc[0]
    assert row['options_setup'] == 'No trade'
    assert row['rejection_code'] == 'REJECTED_INTRADAY_DRIFT'
    assert '6.00%' in row['rejection_detail']


def test_debit_spread_metadata_survives_queue_and_invalid_expiry_is_held(monkeypatch) -> None:
    from types import SimpleNamespace

    import paper_trade
    import src.options_selector as selector

    today = pd.Timestamp.today().normalize()
    target_expiry = (today + pd.Timedelta(days=30)).strftime('%Y-%m-%d')
    late_expiry = (today + pd.Timedelta(days=50)).strftime('%Y-%m-%d')
    signals = pd.DataFrame([
        {'ticker': 'BULL', 'bias': 'bullish', 'suggested_structure': 'Bull call spread',
         'last_close': 100.0, 'signal_score': 5, 'annualized_volatility': 0.2},
        {'ticker': 'BEAR', 'bias': 'bearish', 'suggested_structure': 'Bear put spread',
         'last_close': 100.0, 'signal_score': 5, 'annualized_volatility': 0.2},
        {'ticker': 'LATE', 'bias': 'bullish', 'suggested_structure': 'Bull call spread',
         'last_close': 100.0, 'signal_score': 5, 'annualized_volatility': 0.2},
    ])
    monkeypatch.setattr(selector.yf, 'Ticker', lambda ticker: SimpleNamespace(
        options=[late_expiry] if ticker == 'LATE' else [late_expiry, target_expiry],
    ))
    monkeypatch.setattr(selector, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(paper_trade, '_preserve_existing_state', lambda queue: queue)
    monkeypatch.setattr(paper_trade, '_apply_discovery_quality_gate', lambda queue: queue.assign(
        discovery_quality_score=0.0, discovery_quality_status='unknown',
        discovery_quality_formula_version='', discovery_quality_gate_reason='',
    ))

    candidates = selector.build_options_candidates(signals, pd.DataFrame(), quote_client=object())
    queue = build_paper_trade_queue(candidates, existing_tickers=set(), min_projected_return=-100)
    by_ticker = queue.set_index('ticker')
    for ticker, spread_type, option_type, short_strike in [
        ('BULL', 'bull_call_debit', 'call', 103),
        ('BEAR', 'bear_put_debit', 'put', 97),
    ]:
        row = by_ticker.loc[ticker]
        assert (row['spread_type'], row['spread_option_type'], row['spread_ratio_qty']) == (spread_type, option_type, 1)
        assert (row['spread_long_strike'], row['spread_short_strike']) == (100, short_strike)
        assert row['expiration_target'] == target_expiry
        assert row['days_to_expiration'] == 30
    held = by_ticker.loc['LATE']
    assert held['spread_type'] == 'bull_call_debit'
    assert held['rejection_code'] == 'REJECTED_SPREAD_EXPIRATION'
    assert held['approval_status'] == 'hold'
    assert not bool(held['approved_for_submit'])


def test_rejected_candidate_remains_on_hold_in_queue() -> None:
    candidate = pd.DataFrame([{'ticker': 'NVDA', 'bias': 'bullish', 'options_setup': 'Buy 100C',
                               'signal_score': 5, 'projected_return_pct': 10, 'allocation_pct': 1,
                               'rank_score': 5, 'max_risk_usd': 100,
                               'rejection_code': 'REJECTED_INTRADAY_DRIFT', 'rejection_detail': 'Drift 6.00%'}])
    queue = build_paper_trade_queue(candidate, existing_tickers=set())
    assert len(queue) == 1
    assert queue.iloc[0]['rejection_code'] == 'REJECTED_INTRADAY_DRIFT'
    assert queue.iloc[0]['approval_status'] == 'hold'
    assert not bool(queue.iloc[0]['approved_for_submit'])


def test_option_preview_logs_drift_rejection(monkeypatch) -> None:
    from src.execution import submit_orders

    monkeypatch.setattr('src.market_data.stock_quote_midpoint', lambda *args: 106.0)
    monkeypatch.setenv('MAX_INTRADAY_DRIFT_PCT', '0.05')
    queue = pd.DataFrame([{'ticker': 'NVDA', 'signal_close': 100.0, 'options_setup': 'Buy 100C',
                           'bias': 'bullish', 'approval_status': 'approved', 'approved_for_submit': True}])
    config = TradingConfig(trading_mode='paper', auto_submit=True)
    preview = prepare_option_execution_preview(queue, config, contracts_map={}, quote_client=object())
    assert preview.iloc[0]['rejection_code'] == 'REJECTED_INTRADAY_DRIFT'
    assert '6.00%' in preview.iloc[0]['rejection_detail']
    assert submit_orders(preview, config).iloc[0]['rejection_code'] == 'REJECTED_INTRADAY_DRIFT'


def test_option_snapshot_metrics_rejects_invalid_quotes() -> None:
    from types import SimpleNamespace

    import pytest

    from src.market_data import option_snapshot_metrics

    contract = 'NVDA261218C00100000'
    quote = SimpleNamespace(bid_price=0.97, ask_price=1.03, timestamp=datetime.now(timezone.utc))
    snapshot = SimpleNamespace(latest_quote=quote, implied_volatility=0.5)

    class FakeClient:
        def get_option_snapshot(self, request):
            return {contract: snapshot}

    midpoint, spread, implied_volatility = option_snapshot_metrics(FakeClient(), contract)
    assert midpoint == 1.0
    assert abs(spread - 0.06) < 1e-10
    assert implied_volatility == 0.5
    quote.timestamp -= timedelta(minutes=5)
    with pytest.raises(ValueError, match='stale'):
        option_snapshot_metrics(FakeClient(), contract)
    quote.timestamp = datetime.now(timezone.utc)
    quote.bid_price = 0
    with pytest.raises(ValueError, match='positive'):
        option_snapshot_metrics(FakeClient(), contract)


def test_earnings_calendar_caches_empty_results_and_fails_closed(monkeypatch, tmp_path: Path) -> None:
    import pytest

    import src.catalyst_events as events

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {'earningsCalendar': []}

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        return FakeResponse()

    monkeypatch.setattr(events.requests, 'get', fake_get)
    database = tmp_path / 'calendar.sqlite'
    today = datetime.now(timezone.utc).date()
    assert events.get_earnings_dates(database, 'NVDA', 'fake-key', today) == []
    assert events.get_earnings_dates(database, 'NVDA', 'fake-key', today) == []
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match='credentials unavailable'):
        events.get_earnings_dates(database, 'NVDA', '', today)
    monkeypatch.setattr(events.requests, 'get', lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout()))
    with pytest.raises(RuntimeError, match='lookup failed'):
        events.get_earnings_dates(database, 'AMD', 'fake-key', today)


def test_recent_news_is_tagged_deduplicated_and_fail_closed() -> None:
    from types import SimpleNamespace

    import pytest

    from src.catalyst_events import count_recent_headlines

    now = datetime.now(timezone.utc)
    article = SimpleNamespace(id=1, symbols=['NVDA'], created_at=now - timedelta(minutes=5))

    class FakeNews:
        def get_news(self, request):
            return SimpleNamespace(data={'news': [article, article]}, next_page_token=None)

    assert count_recent_headlines(FakeNews(), 'NVDA', now) == 1
    article.created_at = now - timedelta(minutes=61)
    assert count_recent_headlines(FakeNews(), 'NVDA', now) == 0

    class FailedNews:
        def get_news(self, request):
            raise requests.Timeout()

    with pytest.raises(RuntimeError, match='news lookup failed'):
        count_recent_headlines(FailedNews(), 'NVDA', now)


def test_live_option_guard_passes_and_rejects_all_conditions(monkeypatch) -> None:
    from zoneinfo import ZoneInfo

    import alpaca.data.historical as clients
    import src.catalyst_events as events
    import src.iv_history as history
    import src.market_data as market_data
    from src.execution import validate_live_option_entry

    for name in ('StockHistoricalDataClient', 'OptionHistoricalDataClient', 'NewsClient'):
        monkeypatch.setattr(clients, name, lambda *args, **kwargs: object())
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda *args: (1.0, 0.02, 0.5))
    monkeypatch.setattr(events, 'get_earnings_dates', lambda *args: [])
    monkeypatch.setattr(events, 'count_recent_headlines', lambda *args: 0)
    monkeypatch.setattr(history, 'calculate_iv_rank', lambda *args: (50.0, 30))
    monkeypatch.setattr('src.execution.fetch_option_contracts', lambda *args, **kwargs: pd.DataFrame([{'symbol': 'NVDA'}]))
    monkeypatch.setattr('src.execution.choose_best_option_contract', lambda *args, **kwargs: {'symbol': 'NVDA', 'strike_price': 100.0})
    monkeypatch.setenv('MAX_INTRADAY_DRIFT_PCT', '0.05')
    monkeypatch.setenv('MAX_OPTION_SPREAD_PCT', '0.06')
    monkeypatch.setenv('MAX_SINGLE_LEG_IV_RANK', '80')
    order = pd.Series({'ticker': 'NVDA261218C00100000', 'underlying_ticker': 'NVDA',
                       'signal_close': 100.0, 'strike_price': 100.0, 'days_to_expiration': 30,
                       'options_setup': 'Buy 100C', 'proxy_qty': 1, 'max_risk_usd': 200.0})
    config = TradingConfig(api_key='fake', secret_key='fake')
    assert validate_live_option_entry(order, config)[0] == ''
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 106.0)
    assert validate_live_option_entry(order, config)[0] == 'REJECTED_INTRADAY_DRIFT'
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda *args: (1.0, 0.07, 0.5))
    assert validate_live_option_entry(order, config)[0] == 'REJECTED_SPREAD_WIDTH'
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda *args: (1.0, 0.02, 0.5))
    today = datetime.now(ZoneInfo('America/New_York')).date()
    monkeypatch.setattr(events, 'get_earnings_dates', lambda *args: [today + timedelta(days=7)])
    assert validate_live_option_entry(order, config)[0] == 'REJECTED_CATALYST_BLACKOUT'
    monkeypatch.setattr(events, 'get_earnings_dates', lambda *args: [])
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 104.5)
    monkeypatch.setattr(events, 'count_recent_headlines', lambda *args: 3)
    assert validate_live_option_entry(order, config)[0] == 'REJECTED_CATALYST_BLACKOUT'
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(events, 'count_recent_headlines', lambda *args: 0)
    monkeypatch.setattr(history, 'calculate_iv_rank', lambda *args: (81.0, 30))
    assert validate_live_option_entry(order, config)[0] == 'REJECTED_IV_RANK'
    monkeypatch.delenv('REQUIRE_STRICT_IV_RANK', raising=False)
    monkeypatch.setattr(history, 'calculate_iv_rank', lambda *args: (None, 19))
    monkeypatch.setattr(market_data, 'fetch_history', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('unexpected RV fallback')))
    code, detail, source = validate_live_option_entry(order, config)
    assert code == 'REJECTED_SAFETY_DATA_UNAVAILABLE'
    assert '19 historical observations' in detail
    assert source == 'iv_history'
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: (_ for _ in ()).throw(requests.Timeout()))
    assert validate_live_option_entry(order, config)[0] == 'REJECTED_SAFETY_DATA_UNAVAILABLE'


def test_submission_preserves_guard_rejections_and_never_calls_broker(monkeypatch) -> None:
    import src.execution as execution

    order = pd.Series({'ticker': 'NVDA261218C00100000', 'underlying_ticker': 'NVDA',
                       'proxy_side': 'buy', 'proxy_qty': 1, 'execution_style': 'single_leg_option_paper',
                       'client_order_id': 'paper-option-NVDA-test', 'approved_for_submit': True,
                       'ready_for_broker_submit': True})
    config = TradingConfig(trading_mode='paper', auto_submit=True, api_key='fake', secret_key='fake')
    monkeypatch.setattr(execution, '_request_with_retries', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('broker called')))
    for rejection in ('REJECTED_INTRADAY_DRIFT', 'REJECTED_SPREAD_WIDTH', 'REJECTED_CATALYST_BLACKOUT',
                      'REJECTED_IV_RANK', 'REJECTED_SAFETY_DATA_UNAVAILABLE'):
        monkeypatch.setattr(execution, 'validate_live_option_entry', lambda *args: (rejection, 'test rejection', ''))
        result = execution.submit_single_order(order, config)
        assert result['status'] == 'blocked'
        assert result['rejection_code'] == rejection


def test_option_entry_uses_fresh_quote_limit_and_exit_remains_market(monkeypatch) -> None:
    from types import SimpleNamespace

    import alpaca.data.historical as clients
    import src.execution as execution
    import src.market_data as market_data

    monkeypatch.setattr(execution, 'validate_live_option_entry', lambda *args: ('', '', 'iv_history'))
    monkeypatch.setattr(clients, 'OptionHistoricalDataClient', lambda *args: object())
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda *args: (1.0, 0.02, 0.5))
    monkeypatch.setenv('MAX_OPTION_SPREAD_PCT', '0.06')
    captured = []

    def fake_request(**kwargs):
        captured.append(kwargs['json_body'])
        return SimpleNamespace(json=lambda: {'status': 'accepted'})

    monkeypatch.setattr(execution, '_request_with_retries', fake_request)
    config = TradingConfig(trading_mode='paper', auto_submit=True, api_key='fake', secret_key='fake')
    order = pd.Series({'ticker': 'NVDA261218C00100000', 'underlying_ticker': 'NVDA',
                       'proxy_side': 'buy', 'proxy_qty': 1, 'execution_style': 'single_leg_option_paper',
                       'client_order_id': 'paper-option-NVDA-test', 'approved_for_submit': True,
                       'ready_for_broker_submit': True, 'max_risk_usd': 200.0})
    assert execution.submit_single_order(order, config)['status'] == 'accepted'
    assert captured[0]['type'] == 'limit'
    assert captured[0]['limit_price'] == '1.01'
    assert captured[0]['position_intent'] == 'buy_to_open'

    exit_order = order.copy()
    exit_order['proxy_side'] = 'sell'
    exit_order['execution_style'] = 'protective_exit_paper'
    assert execution.submit_single_order(exit_order, config)['status'] == 'accepted'
    assert captured[1]['type'] == 'market'
    assert captured[1]['position_intent'] == 'sell_to_close'

    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda *args: (_ for _ in ()).throw(ValueError('stale')))
    assert execution.submit_single_order(order, config)['status'] == 'blocked'
    assert len(captured) == 2
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda *args: (3.0, 0.02, 0.5))
    assert execution.submit_single_order(order, config)['rejection_code'] == 'REJECTED_RISK_BUDGET'
    assert len(captured) == 2


def test_paper_option_cycle_passes_in_preview_only_mode(monkeypatch, tmp_path: Path) -> None:
    import alpaca.data.historical as clients
    import src.catalyst_events as events
    import src.execution as execution
    import src.iv_history as history
    import src.market_data as market_data

    for name in ('StockHistoricalDataClient', 'OptionHistoricalDataClient', 'NewsClient'):
        monkeypatch.setattr(clients, name, lambda *args, **kwargs: object())
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda *args: (1.0, 0.02, 0.5))
    monkeypatch.setattr(events, 'get_earnings_dates', lambda *args: [])
    monkeypatch.setattr(events, 'count_recent_headlines', lambda *args: 0)
    monkeypatch.setattr(history, 'calculate_iv_rank', lambda *args: (50.0, 30))
    monkeypatch.setattr(execution, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setattr(execution, '_request_with_retries', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('broker called')))
    queue = pd.DataFrame([{'ticker': 'NVDA', 'signal_close': 100.0, 'options_setup': 'Buy 100C',
                           'bias': 'bullish', 'approval_status': 'approved', 'approved_for_submit': True,
                           'max_risk_usd': 200.0, 'allocation_pct': 1.0}])
    monkeypatch.setattr(execution, 'load_latest_csv', lambda prefix: queue)
    expiration = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    contract = {'symbol': 'NVDA261218C00100000', 'underlying_symbol': 'NVDA', 'type': 'call',
                'tradable': True, 'expiration_date': expiration, 'strike_price': 100.0, 'close_price': 2.0}
    monkeypatch.setattr(execution, 'fetch_option_contracts', lambda *args, **kwargs: pd.DataFrame([contract]))
    config = TradingConfig(trading_mode='paper', auto_submit=False, api_key='fake', secret_key='fake')
    preview, results = execution.run_option_execution_cycle(config)
    assert preview.iloc[0]['ticker'] == contract['symbol']
    assert results.iloc[0]['status'] == 'preview_only'
    assert results.iloc[0]['iv_rank_source'] == 'iv_history'


def test_unvalidated_option_entry_never_reaches_broker(monkeypatch) -> None:
    import src.execution as execution

    def unexpected_request(*args, **kwargs):
        raise AssertionError('unvalidated option entry reached the broker')

    monkeypatch.setattr(execution, '_request_with_retries', unexpected_request)
    order = pd.Series({
        'ticker': 'NVDA261218C00100000', 'underlying_ticker': 'NVDA',
        'proxy_side': 'buy', 'proxy_qty': 1, 'execution_style': 'single_leg_option_paper',
        'client_order_id': 'paper-option-NVDA-test', 'approved_for_submit': True,
        'ready_for_broker_submit': True,
    })
    config = TradingConfig(trading_mode='paper', auto_submit=True, api_key='test', secret_key='test')
    result = execution.submit_single_order(order, config)
    assert result['status'] == 'blocked'
    assert result['rejection_code'] == 'REJECTED_SAFETY_DATA_UNAVAILABLE'


def test_spread_metadata_and_legacy_setup_never_reach_single_leg_broker(monkeypatch) -> None:
    import src.execution as execution

    monkeypatch.setattr(execution, '_request_with_retries', lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('spread reached the broker'),
    ))
    order = pd.Series({
        'ticker': 'NVDA261218C00100000', 'underlying_ticker': 'NVDA',
        'proxy_side': 'buy', 'proxy_qty': 1, 'execution_style': 'single_leg_option_paper',
        'client_order_id': 'paper-option-NVDA-test', 'approved_for_submit': True,
        'ready_for_broker_submit': True,
    })
    config = TradingConfig(trading_mode='paper', auto_submit=False, api_key='test', secret_key='test')
    for fields in ({'spread_type': 'bull_call_debit'}, {'options_setup': 'Buy 100C/Sell 105C'},
                   {'execution_style': 'multi_leg_option_paper'}):
        candidate = order.copy()
        for key, value in fields.items():
            candidate[key] = value
        result = execution.submit_single_order(candidate, config)
        assert result['status'] == 'preview_only'


def test_paper_debit_vertical_sdk_request_and_credit_sign_offline() -> None:
    from decimal import Decimal

    import pytest
    from alpaca.trading.enums import OrderClass, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest
    from src.execution import build_paper_debit_vertical_request

    config = TradingConfig(trading_mode='paper')
    for long_symbol, short_symbol in [
        ('NVDA261218C00100000', 'NVDA261218C00105000'),
        ('NVDA261218P00100000', 'NVDA261218P00095000'),
    ]:
        request = build_paper_debit_vertical_request(config, long_symbol, short_symbol, 2, Decimal('2.04'), 'paper-test')
        payload = request.model_dump(exclude_none=True, mode='json')
        assert payload['order_class'] == 'mleg'
        assert payload['type'] == 'limit'
        assert payload['qty'] == 2
        assert payload['limit_price'] == 2.04
        assert [(leg['symbol'], leg['ratio_qty'], leg['side'], leg['position_intent']) for leg in payload['legs']] == [
            (long_symbol, 1, 'buy', 'buy_to_open'), (short_symbol, 1, 'sell', 'sell_to_open'),
        ]
        credit = LimitOrderRequest(qty=1, limit_price=-1.25, time_in_force=TimeInForce.DAY,
                                   order_class=OrderClass.MLEG, legs=request.legs)
        assert credit.model_dump(exclude_none=True, mode='json')['limit_price'] == -1.25
        with pytest.raises(ValueError):
            build_paper_debit_vertical_request(config, long_symbol, short_symbol, 1, Decimal('-1.25'), 'paper-test')
    with pytest.raises(ValueError):
        build_paper_debit_vertical_request(TradingConfig(trading_mode='live'),
                                          'NVDA261218C00100000', 'NVDA261218C00105000', 1, Decimal('2.04'), 'test')
    with pytest.raises(ValueError):
        build_paper_debit_vertical_request(config, 'NVDA261218C00100000', 'NVDA261225C00105000',
                                          1, Decimal('2.04'), 'test')


def test_paper_vertical_close_request_uses_close_intents_and_signed_limit() -> None:
    from decimal import Decimal

    import pytest
    from src.execution import build_paper_vertical_close_request

    config = TradingConfig(trading_mode='paper')
    request = build_paper_vertical_close_request(
        config, 'NVDA261218C00100000', 'NVDA261218C00105000', 2, Decimal('-1.25'), 'paper-close-test',
    )
    payload = request.model_dump(exclude_none=True, mode='json')
    assert payload['limit_price'] == -1.25
    assert [(leg['side'], leg['position_intent'], leg['ratio_qty']) for leg in payload['legs']] == [
        ('sell', 'sell_to_close', 1), ('buy', 'buy_to_close', 1),
    ]
    with pytest.raises(ValueError):
        build_paper_vertical_close_request(
            config, 'NVDA261218C00100000', 'NVDA261225C00105000', 1, Decimal('1.25'), 'test',
        )
    with pytest.raises(ValueError):
        build_paper_vertical_close_request(
            TradingConfig(trading_mode='live'), 'NVDA261218C00100000', 'NVDA261218C00105000',
            1, Decimal('1.25'), 'test',
        )


def test_paper_spread_account_guard_requires_permission_and_buying_power(monkeypatch) -> None:
    import src.execution as execution

    config = TradingConfig(trading_mode='paper', api_key='paper', secret_key='paper')
    order = pd.Series({'limit_debit': 2.0, 'proxy_qty': 1, 'spread_risk_budget_usd': 300})
    account = {'status': 'ACTIVE', 'options_approved_level': 3, 'options_trading_level': 3,
               'options_buying_power': '500', 'account_blocked': False, 'trading_blocked': False}
    monkeypatch.setattr(execution, '_fetch_alpaca_json', lambda *args: account)
    assert execution.validate_paper_spread_account(config, order) == ('', '')

    account['options_trading_level'] = 2
    assert execution.validate_paper_spread_account(config, order)[0] == 'REJECTED_OPTIONS_PERMISSION'
    account['options_trading_level'] = 3
    account['options_buying_power'] = '100'
    assert execution.validate_paper_spread_account(config, order)[0] == 'REJECTED_BUYING_POWER'
    account['options_buying_power'] = '500'
    account['status'] = 'SUSPENDED'
    assert execution.validate_paper_spread_account(config, order)[0] == 'REJECTED_ACCOUNT_STATUS'
    account['status'] = 'ACTIVE'
    assert execution.validate_paper_spread_account(TradingConfig(trading_mode='live'), order)[0] == 'REJECTED_PAPER_ACCOUNT_REQUIRED'
    monkeypatch.setattr(execution, '_fetch_alpaca_json', lambda *args: (_ for _ in ()).throw(TimeoutError()))
    assert execution.validate_paper_spread_account(config, order)[0] == 'REJECTED_ACCOUNT_STATE'


def test_option_contract_lookup_paginates_and_rejects_incomplete_chain(monkeypatch) -> None:
    import src.execution as execution

    expiration = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    calls = []

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    def get_page(*args, **kwargs):
        token = kwargs['params'].get('page_token')
        calls.append(token)
        symbol = 'FIRST' if token is None else 'SECOND'
        return Response({'option_contracts': [{'symbol': symbol, 'expiration_date': expiration}],
                         'next_page_token': 'next' if token is None else None})

    monkeypatch.setattr(execution.requests, 'get', get_page)
    config = TradingConfig(trading_mode='paper', api_key='test', secret_key='test')
    contracts = execution.fetch_option_contracts(config, 'NVDA', min_days=21, max_days=45)
    assert calls == [None, 'next']
    assert contracts['symbol'].tolist() == ['FIRST', 'SECOND']

    def repeated_page(*args, **kwargs):
        return Response({'option_contracts': [{'symbol': 'FIRST', 'expiration_date': expiration}],
                         'next_page_token': 'next'})

    monkeypatch.setattr(execution.requests, 'get', repeated_page)
    assert execution.fetch_option_contracts(config, 'NVDA', min_days=21, max_days=45).empty


def test_debit_vertical_previews_are_paired_and_ready_after_guardrails(monkeypatch) -> None:
    import src.execution as execution
    import src.market_data as market_data

    monkeypatch.setattr(execution, 'validate_paper_spread_account', lambda *args: ('', ''))
    monkeypatch.setattr(execution, '_validate_debit_vertical_guardrails', lambda *args: ('', '', 'iv_history'))
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda client, symbol, age: (
        (3.0, 0.02, 0.5) if symbol.endswith('00100000') else (1.0, 0.02, 0.5)
    ))
    expiry = pd.Timestamp.today().normalize() + pd.Timedelta(days=30)
    expiration = expiry.strftime('%Y-%m-%d')
    for spread_type, option_type, bias, short_strike, suffix in [
        ('bull_call_debit', 'call', 'bullish', 105, 'C'),
        ('bear_put_debit', 'put', 'bearish', 95, 'P'),
    ]:
        long_symbol = f"NVDA{expiry.strftime('%y%m%d')}{suffix}00100000"
        short_symbol = f"NVDA{expiry.strftime('%y%m%d')}{suffix}{short_strike * 1000:08d}"
        contracts = pd.DataFrame([
            {'symbol': symbol, 'underlying_symbol': 'NVDA', 'type': option_type, 'tradable': True,
             'expiration_date': expiration, 'strike_price': strike, 'open_interest': 100}
            for symbol, strike in [(long_symbol, 100), (short_symbol, short_strike)]
        ])
        queue = pd.DataFrame([{'ticker': 'NVDA', 'signal_close': 100, 'bias': bias,
                               'options_setup': 'Buy / Sell', 'spread_type': spread_type,
                               'spread_option_type': option_type, 'spread_long_strike': 100,
                               'spread_short_strike': short_strike, 'spread_ratio_qty': 1,
                               'expiration_target': expiration, 'max_risk_usd': 500,
                               'allocation_pct': 1.0, 'approval_status': 'approved',
                               'approved_for_submit': True}])
        config = TradingConfig(trading_mode='paper', auto_submit=True)
        preview = prepare_option_execution_preview(queue, config, contracts_map={'NVDA': contracts},
                                                   quote_client=object(), option_quote_client=object()).iloc[0]
        assert (preview['long_leg_symbol'], preview['short_leg_symbol']) == (long_symbol, short_symbol)
        assert (preview['long_leg_intent'], preview['short_leg_intent']) == ('buy_to_open', 'sell_to_open')
        assert (preview['long_leg_ratio_qty'], preview['short_leg_ratio_qty']) == (1, 1)
        assert preview['indicative_debit'] == 2.04
        assert preview['limit_debit'] == 2.04
        assert preview['proxy_qty'] == 2
        assert preview['estimated_max_loss_usd'] == 408
        assert preview['rejection_code'] == ''
        assert bool(preview['ready_for_broker_submit'])
        repeated = prepare_option_execution_preview(queue, config, contracts_map={'NVDA': contracts},
                                quote_client=object(), option_quote_client=object()).iloc[0]
        assert repeated['client_order_id'] == preview['client_order_id']

        contracts.loc[1, 'expiration_date'] = (expiry + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
        invalid = prepare_option_execution_preview(queue, config, contracts_map={'NVDA': contracts},
                                                   quote_client=object(), option_quote_client=object()).iloc[0]
        assert invalid['rejection_code'] == 'REJECTED_SPREAD_CONTRACTS'
        assert not bool(invalid['ready_for_broker_submit'])

    legacy = queue.drop(columns=['spread_type', 'spread_option_type', 'spread_long_strike',
                                 'spread_short_strike', 'spread_ratio_qty'])
    invalid = prepare_option_execution_preview(legacy, config, contracts_map={'NVDA': contracts},
                                               quote_client=object(), option_quote_client=object()).iloc[0]
    assert invalid['rejection_code'].startswith('REJECTED_')
    assert not bool(invalid['ready_for_broker_submit'])

    for fields, code in [
        ({'spread_reconciliation_status': 'mismatched'}, 'REJECTED_SPREAD_RECONCILIATION'),
        ({'spread_order_id': 'parent-1'}, 'REJECTED_SPREAD_ORDER_EXISTS'),
        ({'option_order_status': 'partially_filled'}, 'REJECTED_SPREAD_ORDER_EXISTS'),
    ]:
        blocked_queue = queue.assign(**fields)
        blocked = prepare_option_execution_preview(blocked_queue, config, contracts_map={'NVDA': contracts},
                                                   quote_client=object(), option_quote_client=object()).iloc[0]
        assert blocked['rejection_code'] == code
        assert not bool(blocked['ready_for_broker_submit'])


def test_debit_vertical_preview_rejects_insufficient_risk_budget(monkeypatch) -> None:
    import src.market_data as market_data

    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda client, symbol, age: (
        (3.0, 0.02, 0.5) if symbol.endswith('00100000') else (1.0, 0.02, 0.5)
    ))
    expiry = pd.Timestamp.today().normalize() + pd.Timedelta(days=30)
    expiration = expiry.strftime('%Y-%m-%d')
    symbols = [f"NVDA{expiry.strftime('%y%m%d')}C00100000", f"NVDA{expiry.strftime('%y%m%d')}C00105000"]
    contracts = pd.DataFrame([
        {'symbol': symbol, 'underlying_symbol': 'NVDA', 'type': 'call', 'tradable': True,
         'expiration_date': expiration, 'strike_price': strike, 'open_interest': 100}
        for symbol, strike in zip(symbols, (100, 105))
    ])
    queue = pd.DataFrame([{'ticker': 'NVDA', 'signal_close': 100, 'bias': 'bullish',
                           'options_setup': 'Buy 100C / Sell 105C', 'spread_type': 'bull_call_debit',
                           'spread_option_type': 'call', 'spread_long_strike': 100, 'spread_short_strike': 105,
                           'spread_ratio_qty': 1, 'expiration_target': expiration,
                           'max_risk_usd': 100, 'allocation_pct': 0.05}])
    preview = prepare_option_execution_preview(queue, TradingConfig(trading_mode='paper'),
                                               contracts_map={'NVDA': contracts}, quote_client=object(),
                                               option_quote_client=object()).iloc[0]
    assert preview['rejection_code'] == 'REJECTED_RISK_BUDGET'
    assert preview['proxy_qty'] == 0
    assert not bool(preview['ready_for_broker_submit'])


def test_debit_vertical_guard_preserves_iv_and_catalyst_gates(monkeypatch) -> None:
    from zoneinfo import ZoneInfo

    import alpaca.data.historical as clients
    import src.catalyst_events as events
    import src.execution as execution
    import src.iv_history as history
    import src.market_data as market_data

    monkeypatch.setattr(clients, 'NewsClient', lambda *args, **kwargs: object())
    monkeypatch.setattr(market_data, 'option_snapshot_metrics', lambda *args: (1.0, 0.02, 0.5))
    monkeypatch.setattr(execution, 'fetch_option_contracts', lambda *args, **kwargs: pd.DataFrame([{'symbol': 'NVDA261218C00100000'}]))
    monkeypatch.setattr(execution, 'choose_best_option_contract', lambda *args, **kwargs: {'symbol': 'NVDA261218C00100000', 'strike_price': 100.0})
    monkeypatch.setenv('MAX_INTRADAY_DRIFT_PCT', '0.05')
    monkeypatch.setenv('MAX_OPTION_SPREAD_PCT', '0.06')
    monkeypatch.setenv('MAX_SINGLE_LEG_IV_RANK', '80')
    monkeypatch.setenv('EARNINGS_BLACKOUT_DAYS', '7')
    monkeypatch.setenv('BREAKING_NEWS_COUNT_THRESHOLD', '3')
    monkeypatch.setenv('BREAKING_NEWS_MOVE_PCT', '0.04')
    today = datetime.now(ZoneInfo('America/New_York')).date()
    monkeypatch.setattr(events, 'get_earnings_dates', lambda *args: [])
    monkeypatch.setattr(events, 'count_recent_headlines', lambda *args: 0)
    monkeypatch.setattr(history, 'calculate_iv_rank', lambda *args: (50.0, 30))
    monkeypatch.setattr(market_data, 'fetch_history', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('strict IV fallback used')))
    config = TradingConfig(trading_mode='paper', api_key='paper-key', secret_key='paper-secret')
    row = pd.Series({'ticker': 'NVDA', 'signal_close': 100.0})

    assert execution._validate_debit_vertical_guardrails(row, config, 100.0, 0.02, object()) == ('', '', 'iv_history')
    monkeypatch.setattr(events, 'get_earnings_dates', lambda *args: [today + timedelta(days=3)])
    assert execution._validate_debit_vertical_guardrails(row, config, 100.0, 0.02, object())[0] == 'REJECTED_CATALYST_BLACKOUT'
    monkeypatch.setattr(events, 'get_earnings_dates', lambda *args: [])
    monkeypatch.setattr(events, 'count_recent_headlines', lambda *args: 3)
    assert execution._validate_debit_vertical_guardrails(row, config, 104.5, 0.02, object())[0] == 'REJECTED_CATALYST_BLACKOUT'
    monkeypatch.setattr(events, 'count_recent_headlines', lambda *args: 0)
    monkeypatch.setattr(history, 'calculate_iv_rank', lambda *args: (81.0, 30))
    assert execution._validate_debit_vertical_guardrails(row, config, 100.0, 0.02, object())[0] == 'REJECTED_IV_RANK'
    monkeypatch.setattr(history, 'calculate_iv_rank', lambda *args: (None, 19))
    assert execution._validate_debit_vertical_guardrails(row, config, 100.0, 0.02, object())[0] == 'REJECTED_SAFETY_DATA_UNAVAILABLE'


def test_guarded_mleg_entry_revalidates_and_submits_debit_limit(monkeypatch) -> None:
    import alpaca.data.historical as clients
    import src.execution as execution
    import src.market_data as market_data

    long_symbol = 'NVDA261218C00100000'
    short_symbol = 'NVDA261218C00105000'
    quote_calls: list[str] = []
    monkeypatch.setattr(clients, 'StockHistoricalDataClient', lambda *args: object())
    monkeypatch.setattr(clients, 'OptionHistoricalDataClient', lambda *args: object())
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)

    def option_quote(_client, symbol, _age):
        quote_calls.append(symbol)
        return (3.0, 0.02, 0.5) if symbol == long_symbol else (1.0, 0.02, 0.5)

    monkeypatch.setattr(market_data, 'option_snapshot_metrics', option_quote)
    guard_calls = []
    monkeypatch.setattr(execution, '_validate_debit_vertical_guardrails',
                        lambda *args: (guard_calls.append(args) or ('', '', 'iv_history')))
    account_calls = []
    monkeypatch.setattr(execution, 'validate_paper_spread_account',
                        lambda _config, fresh: (account_calls.append(fresh.copy()) or ('', '')))
    monkeypatch.setattr(execution, '_lookup_order_by_client_order_id', lambda *_args: None)
    monkeypatch.setattr(execution, '_request_with_retries',
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('generic retry used')))
    submitted = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {'id': 'parent-entry', 'status': 'accepted'}

    def post(**kwargs):
        submitted.append(kwargs)
        return Response()

    monkeypatch.setattr(execution.requests, 'request', post)
    order = pd.Series({
        'ticker': 'NVDA', 'underlying_ticker': 'NVDA', 'proxy_side': 'buy', 'proxy_qty': 1,
        'execution_style': 'multi_leg_option_paper', 'client_order_id': 'paper-spread-NVDA-stable',
        'approved_for_submit': True, 'ready_for_broker_submit': True, 'signal_close': 100.0,
        'spread_risk_budget_usd': 500.0, 'max_risk_usd': 500.0,
        'long_leg_symbol': long_symbol, 'short_leg_symbol': short_symbol,
    })
    config = TradingConfig(trading_mode='paper', auto_submit=True, api_key='paper', secret_key='paper')

    result = execution.submit_single_order(order, config)

    assert result['status'] == 'accepted'
    assert result['limit_price'] == '2.04'
    assert quote_calls.count(long_symbol) == quote_calls.count(short_symbol) == 1
    assert guard_calls and account_calls[0]['limit_debit'] == 2.04
    assert len(submitted) == 1
    payload = submitted[0]['json']
    assert payload['order_class'] == 'mleg'
    assert payload['type'] == 'limit'
    assert payload['limit_price'] == 2.04
    assert [(leg['symbol'], leg['side'], leg['position_intent']) for leg in payload['legs']] == [
        (long_symbol, 'buy', 'buy_to_open'), (short_symbol, 'sell', 'sell_to_open'),
    ]


def test_ambiguous_mleg_response_is_reconciled_without_resubmission(monkeypatch) -> None:
    import alpaca.data.historical as clients
    import src.execution as execution
    import src.market_data as market_data

    long_symbol = 'NVDA261218C00100000'
    short_symbol = 'NVDA261218C00105000'
    monkeypatch.setattr(clients, 'StockHistoricalDataClient', lambda *args: object())
    monkeypatch.setattr(clients, 'OptionHistoricalDataClient', lambda *args: object())
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)
    monkeypatch.setattr(market_data, 'option_snapshot_metrics',
                        lambda _client, symbol, _age: (3.0, 0.02, 0.5) if symbol == long_symbol else (1.0, 0.02, 0.5))
    monkeypatch.setattr(execution, '_validate_debit_vertical_guardrails', lambda *args: ('', '', 'iv_history'))
    monkeypatch.setattr(execution, 'validate_paper_spread_account', lambda *args: ('', ''))
    reconciled_order = {
        'id': 'parent-partial', 'status': 'partially_filled', 'filled_qty': '1',
        'legs': [{'symbol': long_symbol, 'filled_qty': '1'}, {'symbol': short_symbol, 'filled_qty': '1'}],
    }
    lookups = iter([None, reconciled_order])
    monkeypatch.setattr(execution, '_lookup_order_by_client_order_id', lambda *_args: next(lookups))
    posts = []

    def ambiguous_post(**_kwargs):
        posts.append('POST')
        raise requests.Timeout('response timed out after submission')

    monkeypatch.setattr(execution.requests, 'request', ambiguous_post)
    order = pd.Series({
        'ticker': 'NVDA', 'underlying_ticker': 'NVDA', 'proxy_side': 'buy', 'proxy_qty': 1,
        'execution_style': 'multi_leg_option_paper', 'client_order_id': 'paper-spread-NVDA-ambiguous',
        'approved_for_submit': True, 'ready_for_broker_submit': True, 'signal_close': 100.0,
        'spread_risk_budget_usd': 500.0, 'long_leg_symbol': long_symbol, 'short_leg_symbol': short_symbol,
    })
    config = TradingConfig(trading_mode='paper', auto_submit=True, api_key='paper', secret_key='paper')

    result = execution.submit_single_order(order, config)

    assert result['status'] == 'partially_filled'
    assert result['spread_reconciliation_status'] == 'matched'
    assert len(posts) == 1


def test_debit_vertical_preview_rejects_bad_leg_quotes_and_contracts(monkeypatch) -> None:
    from types import SimpleNamespace

    import src.execution as execution
    import src.market_data as market_data

    monkeypatch.setattr(execution, 'validate_paper_spread_account', lambda *args: ('', ''))
    monkeypatch.setattr(execution, '_validate_debit_vertical_guardrails', lambda *args: ('', '', 'iv_history'))
    monkeypatch.setattr(market_data, 'stock_quote_midpoint', lambda *args: 100.0)
    expiry = pd.Timestamp.today().normalize() + pd.Timedelta(days=30)
    expiration = expiry.strftime('%Y-%m-%d')
    long_symbol = f"NVDA{expiry.strftime('%y%m%d')}C00100000"
    short_symbol = f"NVDA{expiry.strftime('%y%m%d')}C00105000"
    contracts = pd.DataFrame([
        {'symbol': symbol, 'underlying_symbol': 'NVDA', 'type': 'call', 'tradable': True,
         'expiration_date': expiration, 'strike_price': strike, 'open_interest': 100}
        for symbol, strike in [(long_symbol, 100), (short_symbol, 105)]
    ])
    queue = pd.DataFrame([{'ticker': 'NVDA', 'signal_close': 100, 'bias': 'bullish',
                           'options_setup': 'Buy 100C / Sell 105C', 'spread_type': 'bull_call_debit',
                           'spread_option_type': 'call', 'spread_long_strike': 100, 'spread_short_strike': 105,
                           'spread_ratio_qty': 1, 'expiration_target': expiration,
                           'max_risk_usd': 500, 'allocation_pct': 1.0}])

    class Quotes:
        def __init__(self):
            self.quotes = {
                long_symbol: SimpleNamespace(bid_price=2.97, ask_price=3.03, timestamp=datetime.now(timezone.utc)),
                short_symbol: SimpleNamespace(bid_price=0.99, ask_price=1.01, timestamp=datetime.now(timezone.utc)),
            }

        def get_option_snapshot(self, request):
            symbol = request.symbol_or_symbols
            return {symbol: SimpleNamespace(latest_quote=self.quotes[symbol], implied_volatility=0.5)}

    def preview(frame, quotes):
        return prepare_option_execution_preview(queue, TradingConfig(trading_mode='paper'),
                                                contracts_map={'NVDA': frame}, quote_client=object(),
                                                option_quote_client=quotes).iloc[0]

    quotes = Quotes()
    assert preview(contracts, quotes)['rejection_code'] == ''
    quotes.quotes[short_symbol].timestamp -= timedelta(minutes=5)
    assert preview(contracts, quotes)['rejection_code'] == 'REJECTED_SAFETY_DATA_UNAVAILABLE'
    quotes = Quotes()
    quotes.quotes[short_symbol].ask_price = 0.9
    assert preview(contracts, quotes)['rejection_code'] == 'REJECTED_SAFETY_DATA_UNAVAILABLE'
    quotes = Quotes()
    quotes.quotes[short_symbol].ask_price = 1.2
    assert preview(contracts, quotes)['rejection_code'] == 'REJECTED_SPREAD_WIDTH'
    for column, value in [('tradable', False), ('open_interest', 0), ('symbol', long_symbol)]:
        invalid = contracts.copy()
        invalid.loc[1, column] = value
        assert preview(invalid, Quotes())['rejection_code'] == 'REJECTED_SPREAD_CONTRACTS'


def test_blocked_option_reason_is_recorded_in_queue(monkeypatch, tmp_path: Path) -> None:
    import src.execution as execution

    monkeypatch.setattr(execution, 'OUTPUT_DIR', tmp_path)
    pd.DataFrame([{'ticker': 'NVDA'}]).to_csv(tmp_path / 'paper_trade_queue.csv', index=False)
    execution.update_queue_after_execution(pd.DataFrame([{
        'underlying_ticker': 'NVDA', 'status': 'blocked', 'execution_style': 'single_leg_option_paper',
        'rejection_code': 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'detail': 'Validation unavailable',
    }]))
    row = pd.read_csv(tmp_path / 'paper_trade_queue.csv').iloc[0]
    assert row['option_order_status'] == 'blocked'
    assert row['rejection_code'] == 'REJECTED_SAFETY_DATA_UNAVAILABLE'
    assert row['rejection_detail'] == 'Validation unavailable'


def test_bearish_breakdown_gets_ranked_for_shorts() -> None:
    market_data = {
        'BEAR': _make_market_frame([120, 118, 117, 116, 115, 114, 112, 111, 109, 108, 107, 106, 105, 104, 103, 101, 100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 89, 88, 87, 86, 85, 84, 83, 82]),
    }

    report = build_signal_report(market_data)
    row = report.iloc[0]

    assert row['bias'] == 'bearish'
    assert float(row['short_rank_score']) > float(row['long_rank_score'])
    assert row['suggested_structure'] in {'Bear put spread', 'Long put'}


def test_prepare_exit_execution_preview_requires_guardrails(monkeypatch) -> None:
    monkeypatch.setenv('ENABLE_EXIT_AUTOMATION', 'false')

    recommendations = pd.DataFrame(
        [
            {
                'symbol': 'SPY',
                'side': 'long',
                'qty': 4,
                'action': 'stop_out',
                'reason': 'Loss breached threshold.',
                'exit_approved': True,
                'order_status': 'monitor',
            }
        ]
    )
    positions = pd.DataFrame(
        [
            {
                'symbol': 'SPY',
                'side': 'long',
                'qty': 4,
                'market_value': 2000.0,
                'unrealized_pl': -60.0,
            }
        ]
    )

    config = TradingConfig(auto_submit=True, trading_mode='paper')
    preview = prepare_exit_execution_preview(recommendations, positions, config)

    assert not preview.empty
    row = preview.iloc[0]
    assert row['exit_side'] == 'sell'
    assert int(row['exit_qty']) == 4
    assert bool(row['ready_for_broker_submit']) is False


def test_import_raw_trade_data_normalizes_common_columns(tmp_path: Path) -> None:
    raw_dir = tmp_path / 'congress_raw'
    raw_dir.mkdir()
    pd.DataFrame(
        [
            {
                'Representative': 'Jane Doe',
                'Ticker': 'NVDA',
                'Transaction': 'Purchase',
                'Transaction Date': '2026-04-10',
                'Notification Date': '2026-04-15',
                'Amount': '$15,001 - $50,000',
            }
        ]
    ).to_csv(raw_dir / 'house_sample.csv', index=False)

    imported = import_raw_trade_data(tmp_path)

    assert len(imported) == 1
    row = imported.iloc[0]
    assert row['member'] == 'Jane Doe'
    assert row['ticker'] == 'NVDA'
    assert row['transaction_type'] == 'Purchase'


def test_build_recent_large_trades_filters_by_window_and_size() -> None:
    disclosures = pd.DataFrame(
        [
            {'chamber': 'House', 'member': 'Nancy Pelosi', 'ticker': 'NVDA', 'transaction_type': 'Purchase', 'transaction_date': '2026-04-10', 'disclosed_date': '2026-04-15', 'amount_range': '$100,001 - $250,000', 'source_note': 'test'},
            {'chamber': 'House', 'member': 'Jane Doe', 'ticker': 'AAPL', 'transaction_type': 'Purchase', 'transaction_date': '2026-03-01', 'disclosed_date': '2026-03-10', 'amount_range': '$1,001 - $15,000', 'source_note': 'test'},
        ]
    )

    recent = build_recent_large_trades(disclosures, lookback_days=14, min_amount_usd=50000, reference_date='2026-04-17')

    assert len(recent) == 1
    assert recent.iloc[0]['member'] == 'Nancy Pelosi'
    assert recent.iloc[0]['ticker'] == 'NVDA'


def test_build_public_interest_watchlist_surfaces_pelosi() -> None:
    disclosures = pd.DataFrame(
        [
            {'chamber': 'House', 'member': 'Nancy Pelosi', 'ticker': 'NVDA', 'transaction_type': 'Purchase', 'transaction_date': '2026-04-10', 'disclosed_date': '2026-04-15', 'amount_range': '$100,001 - $250,000', 'source_note': 'test'},
        ]
    )

    watchlist = build_public_interest_watchlist(disclosures, reference_date='2026-04-17')

    assert not watchlist.empty
    assert 'Nancy Pelosi' in watchlist['member'].tolist()
    assert int(watchlist.loc[watchlist['member'] == 'Nancy Pelosi', 'recent_trade_count'].iloc[0]) == 1


def test_choose_best_option_contract_prefers_short_dated_tradable_match() -> None:
    today = datetime.now(timezone.utc).date()
    near_expiry = (today + timedelta(days=7)).isoformat()
    mid_expiry = (today + timedelta(days=14)).isoformat()
    far_expiry = (today + timedelta(days=35)).isoformat()

    contracts = pd.DataFrame(
        [
            {'symbol': 'AMD260417C00270000', 'underlying_symbol': 'AMD', 'type': 'call', 'tradable': True, 'expiration_date': near_expiry, 'strike_price': '270'},
            {'symbol': 'AMD260424C00280000', 'underlying_symbol': 'AMD', 'type': 'call', 'tradable': True, 'expiration_date': mid_expiry, 'strike_price': '280'},
            {'symbol': 'AMD260515C00290000', 'underlying_symbol': 'AMD', 'type': 'call', 'tradable': True, 'expiration_date': far_expiry, 'strike_price': '290'},
        ]
    )

    chosen = choose_best_option_contract(contracts, underlying_symbol='AMD', option_type='call', target_strike=279, min_days=1, max_days=10)

    assert chosen is not None
    assert chosen['symbol'] == 'AMD260417C00270000'


def test_prepare_option_execution_preview_uses_contract_symbol(monkeypatch) -> None:
    monkeypatch.setattr('src.market_data.stock_quote_midpoint', lambda *args: 278.0)
    today = datetime.now(timezone.utc).date()
    near_expiry = (today + timedelta(days=7)).isoformat()

    queue = pd.DataFrame(
        [
            {
                'ticker': 'AMD',
                'signal_close': 278.0,
                'bias': 'bullish',
                'options_setup': 'Buy 278C',
                'signal_score': 10,
                'projected_return_pct': 117.86,
                'max_risk_usd': 413.10,
                'allocation_pct': 1.2,
                'approval_status': 'approved',
                'approved_for_submit': True,
                'order_status': 'queued_for_review',
            }
        ]
    )
    contracts = {
        'AMD': pd.DataFrame(
            [
                {'symbol': 'AMD260424C00280000', 'underlying_symbol': 'AMD', 'type': 'call', 'tradable': True, 'expiration_date': near_expiry, 'strike_price': '280'}
            ]
        )
    }

    config = TradingConfig(
        auto_submit=True,
        trading_mode='paper',
        use_proxy_equities=False,
        min_option_dte=1,
        max_option_dte=10,
        target_option_dte_min=1,
        target_option_dte_max=10,
    )
    preview = prepare_option_execution_preview(queue, config, contracts_map=contracts, max_days=10, quote_client=object())

    assert not preview.empty
    row = preview.iloc[0]
    assert row['ticker'] == 'AMD260424C00280000'
    assert row['execution_style'] == 'single_leg_option_paper'
    assert bool(row['ready_for_broker_submit']) is True


def test_prepare_option_execution_preview_skips_large_strike_mismatch(monkeypatch) -> None:
    monkeypatch.setattr('src.market_data.stock_quote_midpoint', lambda *args: 624.0)
    today = datetime.now(timezone.utc).date()
    target_expiry = (today + timedelta(days=16)).isoformat()

    queue = pd.DataFrame(
        [
            {
                'ticker': 'META',
                'signal_close': 624.0,
                'bias': 'bullish',
                'options_setup': 'Buy 624C',
                'signal_score': 5,
                'projected_return_pct': 97.44,
                'max_risk_usd': 883.12,
                'allocation_pct': 1.2,
                'approval_status': 'approved',
                'approved_for_submit': True,
                'order_status': 'queued_for_review',
            }
        ]
    )
    contracts = {
        'META': pd.DataFrame(
            [
                {
                    'symbol': 'META260710C00390000',
                    'underlying_symbol': 'META',
                    'type': 'call',
                    'tradable': True,
                    'expiration_date': target_expiry,
                    'strike_price': '390',
                    'close_price': '233.55',
                }
            ]
        )
    }

    config = TradingConfig(
        auto_submit=True,
        trading_mode='paper',
        use_proxy_equities=False,
        min_option_dte=1,
        max_option_dte=30,
        target_option_dte_min=1,
        target_option_dte_max=30,
    )
    preview = prepare_option_execution_preview(queue, config, contracts_map=contracts, max_days=30, quote_client=object())

    assert preview.empty


def test_prepare_option_execution_preview_skips_contracts_over_risk_budget(monkeypatch) -> None:
    monkeypatch.setattr('src.market_data.stock_quote_midpoint', lambda *args: 220.0)
    today = datetime.now(timezone.utc).date()
    target_expiry = (today + timedelta(days=16)).isoformat()

    queue = pd.DataFrame(
        [
            {
                'ticker': 'AAPL',
                'signal_close': 220.0,
                'bias': 'bullish',
                'options_setup': 'Buy 220C',
                'signal_score': 9,
                'projected_return_pct': 32.0,
                'max_risk_usd': 250.0,
                'allocation_pct': 3.0,
                'approval_status': 'approved',
                'approved_for_submit': True,
                'order_status': 'queued_for_review',
            }
        ]
    )
    contracts = {
        'AAPL': pd.DataFrame(
            [
                {
                    'symbol': 'AAPL260717C00220000',
                    'underlying_symbol': 'AAPL',
                    'type': 'call',
                    'tradable': True,
                    'expiration_date': target_expiry,
                    'strike_price': '220',
                    'close_price': '4.00',
                }
            ]
        )
    }

    config = TradingConfig(
        auto_submit=True,
        trading_mode='paper',
        use_proxy_equities=False,
        account_size_usd=100000.0,
        min_option_dte=1,
        max_option_dte=30,
        target_option_dte_min=1,
        target_option_dte_max=30,
    )
    preview = prepare_option_execution_preview(queue, config, contracts_map=contracts, max_days=30, quote_client=object())

    assert preview.empty


def test_prepare_equity_execution_preview_auto_approves_in_automatic_mode() -> None:
    queue = pd.DataFrame(
        [
            {
                'ticker': 'SNAP',
                'bias': 'bullish',
                'options_setup': 'Buy 6C',
                'signal_score': 8,
                'projected_return_pct': 42.0,
                'max_risk_usd': 200.0,
                'allocation_pct': 1.0,
                'approval_status': 'pending',
                'approved_for_submit': False,
                'order_status': 'queued_for_review',
            }
        ]
    )
    signals = pd.DataFrame([{'ticker': 'SNAP', 'last_close': 10.0}])

    config = TradingConfig(auto_submit=True, trading_mode='paper', approval_mode='automatic')
    preview = prepare_equity_execution_preview(queue, signals, config)

    assert not preview.empty
    row = preview.iloc[0]
    assert row['approval_status'] == 'auto_approved'
    assert bool(row['approved_for_submit']) is True
    assert bool(row['ready_for_broker_submit']) is True


def test_prepare_option_execution_preview_auto_approves_paper_rows_when_safe(monkeypatch) -> None:
    monkeypatch.setenv('AUTO_APPROVE_PAPER_TRADES', 'true')
    monkeypatch.setenv('DISCOVERY_QUALITY_STRICT', 'false')
    monkeypatch.setattr('src.market_data.stock_quote_midpoint', lambda *args: 220.0)

    today = datetime.now(timezone.utc).date()
    target_expiry = (today + timedelta(days=16)).isoformat()

    queue = pd.DataFrame(
        [
            {
                'ticker': 'AAPL',
                'signal_close': 220.0,
                'bias': 'bullish',
                'options_setup': 'Buy 220C',
                'signal_score': 9,
                'projected_return_pct': 32.0,
                'max_risk_usd': 1000.0,
                'allocation_pct': 10.0,
                'approval_status': 'pending',
                'approved_for_submit': False,
                'order_status': 'queued_for_review',
                'guardrail_status': 'pass',
                'discovery_quality_status': 'block',
                'discovery_quality_override': False,
            }
        ]
    )
    contracts = {
        'AAPL': pd.DataFrame(
            [
                {
                    'symbol': 'AAPL260717C00220000',
                    'underlying_symbol': 'AAPL',
                    'type': 'call',
                    'tradable': True,
                    'expiration_date': target_expiry,
                    'strike_price': '220',
                    'close_price': '4.00',
                }
            ]
        )
    }

    config = TradingConfig(
        auto_submit=True,
        trading_mode='paper',
        use_proxy_equities=False,
        account_size_usd=100000.0,
        min_option_dte=1,
        max_option_dte=30,
        target_option_dte_min=1,
        target_option_dte_max=30,
    )
    preview = prepare_option_execution_preview(queue, config, contracts_map=contracts, max_days=30, quote_client=object())

    assert not preview.empty
    row = preview.iloc[0]
    assert row['approval_status'] == 'auto_approved'
    assert bool(row['approved_for_submit']) is True
    assert bool(row['ready_for_broker_submit']) is True


def test_build_paper_trade_queue_skips_existing_positions() -> None:
    candidates = pd.DataFrame(
        [
            {'ticker': 'QQQ', 'options_setup': 'Buy 650C', 'signal_score': 9, 'projected_return_pct': 40.0, 'allocation_pct': 1.0, 'rank_score': 99.0, 'max_risk_usd': 500.0, 'bias': 'bullish'},
            {'ticker': 'SNAP', 'options_setup': 'Buy 6C', 'signal_score': 8, 'projected_return_pct': 35.0, 'allocation_pct': 1.0, 'rank_score': 95.0, 'max_risk_usd': 300.0, 'bias': 'bullish'},
        ]
    )

    queue = build_paper_trade_queue(candidates, existing_tickers={'QQQ'}, max_positions=5)

    assert list(queue['ticker']) == ['SNAP']


def test_build_exit_recommendations_includes_option_close_plan() -> None:
    positions = pd.DataFrame(
        [
            {
                'symbol': 'AMD260424C00277500',
                'side': 'long',
                'qty': 1,
                'market_value': 755.0,
                'unrealized_pl': -10.0,
            }
        ]
    )
    signals = pd.DataFrame(
        [
            {
                'ticker': 'AMD',
                'signal_score': 8,
                'bias': 'bullish',
            }
        ]
    )

    recommendations = build_exit_recommendations(positions, signals)

    assert len(recommendations) == 1
    row = recommendations.iloc[0]
    assert row['asset_class'] == 'option'
    assert row['underlying_symbol'] == 'AMD'
    assert 'close' in str(row['close_or_exercise_plan']).lower() or 'exercise' in str(row['close_or_exercise_plan']).lower()


def test_exit_recommendations_keep_healthy_verticals_paired() -> None:
    positions = pd.DataFrame([
        {'symbol': 'NVDA261218C00100000', 'side': 'long', 'qty': 2, 'market_value': 400.0, 'unrealized_pl': -20.0},
        {'symbol': 'NVDA261218C00105000', 'side': 'short', 'qty': 2, 'market_value': -150.0, 'unrealized_pl': 15.0},
        {'symbol': 'AMD261218C00100000', 'side': 'long', 'qty': 1, 'market_value': 100.0, 'unrealized_pl': -10.0},
    ])
    signals = pd.DataFrame([
        {'ticker': 'NVDA', 'signal_score': -2, 'bias': 'bearish'},
        {'ticker': 'AMD', 'signal_score': -2, 'bias': 'bearish'},
    ])

    recommendations = build_exit_recommendations(positions, signals).set_index('symbol')
    for symbol in ('NVDA261218C00100000', 'NVDA261218C00105000'):
        row = recommendations.loc[symbol]
        assert row['action'] in {'reduce_or_close', 'stop_out'}
        assert row['spread_lifecycle_status'] == 'healthy_matched'
        assert row['exit_pct'] == 100
        assert not bool(row['exit_approved'])
        assert 'together' in row['close_or_exercise_plan']
        assert row['paired_symbols'] == 'NVDA261218C00100000,NVDA261218C00105000'
    standalone = recommendations.loc['AMD261218C00100000']
    assert standalone['spread_lifecycle_status'] == 'standalone'
    assert standalone['action'] in {'reduce_or_close', 'stop_out'}


def test_paired_close_preview_and_execution_use_one_mleg_order(monkeypatch) -> None:
    import alpaca.data.historical as clients
    import src.execution as execution
    import src.market_data as market_data

    long_symbol = 'NVDA261218C00100000'
    short_symbol = 'NVDA261218C00105000'
    positions = pd.DataFrame([
        {'symbol': long_symbol, 'side': 'long', 'qty': 2, 'market_value': 400.0, 'unrealized_pl': -20.0},
        {'symbol': short_symbol, 'side': 'short', 'qty': 2, 'market_value': -150.0, 'unrealized_pl': 15.0},
    ])
    signals = pd.DataFrame([{'ticker': 'NVDA', 'signal_score': -2, 'bias': 'bearish'}])
    recommendations = build_exit_recommendations(positions, signals)
    recommendations = apply_automatic_exit_guardrails(recommendations, enable_auto_approve=True)
    config = TradingConfig(trading_mode='paper', auto_submit=True, api_key='paper', secret_key='paper')
    monkeypatch.setenv('ENABLE_EXIT_AUTOMATION', 'true')
    monkeypatch.setattr(clients, 'OptionHistoricalDataClient', lambda *args: object())
    monkeypatch.setattr(market_data, 'option_snapshot_metrics',
                        lambda _client, symbol, _age: (3.0, 0.02, 0.5) if symbol == long_symbol else (1.0, 0.02, 0.5))
    preview = prepare_exit_execution_preview(
        recommendations, positions, config, option_quote_client=object(),
    )
    assert len(preview) == 1
    close_order = preview.iloc[0]
    assert close_order['execution_style'] == 'protective_exit_mleg_paper'
    assert close_order['spread_lifecycle_status'] == 'healthy_matched'
    assert close_order['long_leg_symbol'] == long_symbol
    assert close_order['short_leg_symbol'] == short_symbol
    assert close_order['limit_price'] == -1.96
    assert bool(close_order['ready_for_broker_submit'])

    monkeypatch.setattr(execution, '_lookup_order_by_client_order_id', lambda *_args: None)
    def fake_fetch(_config, endpoint):
        if endpoint == '/v2/positions':
            return [
                {'symbol': long_symbol, 'side': 'long', 'qty': '2'},
                {'symbol': short_symbol, 'side': 'short', 'qty': '2'},
            ]
        if endpoint.startswith('/v2/orders'):
            return []
        raise AssertionError(endpoint)

    monkeypatch.setattr(execution, '_fetch_alpaca_json', fake_fetch)
    submitted = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {'id': 'parent-close', 'status': 'accepted'}

    monkeypatch.setattr(execution.requests, 'request', lambda **kwargs: submitted.append(kwargs) or Response())
    result = execution.submit_single_order(close_order, config)

    assert result['status'] == 'accepted'
    assert len(submitted) == 1
    payload = submitted[0]['json']
    assert payload['order_class'] == 'mleg'
    assert payload['type'] == 'limit'
    assert payload['limit_price'] == -1.96
    assert [(leg['symbol'], leg['side'], leg['position_intent']) for leg in payload['legs']] == [
        (long_symbol, 'sell', 'sell_to_close'), (short_symbol, 'buy', 'buy_to_close'),
    ]


def test_degraded_spreads_halt_and_alert_without_close_submission(monkeypatch, capsys) -> None:
    import src.execution as execution

    long_symbol = 'NVDA261218C00100000'
    short_symbol = 'NVDA261218C00105000'
    base_positions = [
        {'symbol': long_symbol, 'side': 'long', 'qty': 2, 'market_value': 400.0, 'unrealized_pl': -20.0},
        {'symbol': short_symbol, 'side': 'short', 'qty': 2, 'market_value': -150.0, 'unrealized_pl': 15.0},
    ]
    signals = pd.DataFrame([{'ticker': 'NVDA', 'signal_score': -2, 'bias': 'bearish'}])
    partial_order = pd.DataFrame([{
        'order_id': 'parent-nvda', 'client_order_id': 'paper-spread-NVDA-stable',
        'underlying_symbol': 'NVDA', 'order_class': 'mleg', 'status': 'partially_filled',
        'filled_qty': 1,
        'legs_json': json.dumps([{'symbol': long_symbol}, {'symbol': short_symbol}]),
        'spread_reconciliation_status': 'matched',
    }])
    degraded_cases = [
        (pd.DataFrame(base_positions), partial_order),
        (pd.DataFrame([base_positions[0]]), partial_order),
        (pd.DataFrame([base_positions[0], {**base_positions[1], 'qty': 1}]), pd.DataFrame()),
        (pd.DataFrame([base_positions[0], {**base_positions[1], 'early_assignment_flag': True}]), pd.DataFrame()),
    ]
    for positions, orders in degraded_cases:
        recommendations = build_exit_recommendations(positions, signals, orders)
        assert not recommendations.empty
        assert recommendations['spread_lifecycle_status'].astype(str).eq('MANUAL_REVIEW_REQUIRED').all()
        assert recommendations['action'].astype(str).eq('hold').all()
        assert recommendations['spread_alert_required'].map(bool).all()
        assert prepare_exit_execution_preview(
            recommendations, positions, TradingConfig(trading_mode='paper', auto_submit=True),
        ).empty
    stale_recommendations = pd.DataFrame([
        {'symbol': long_symbol, 'action': 'stop_out', 'exit_approved': True, 'exit_pct': 100},
        {'symbol': short_symbol, 'action': 'stop_out', 'exit_approved': True, 'exit_pct': 100},
    ])
    stale_preview = prepare_exit_execution_preview(
        stale_recommendations, pd.DataFrame(base_positions),
        TradingConfig(trading_mode='paper', auto_submit=True),
    )
    assert len(stale_preview) == 1
    assert stale_preview.iloc[0]['spread_lifecycle_status'] == 'MANUAL_REVIEW_REQUIRED'
    assert not bool(stale_preview.iloc[0]['ready_for_broker_submit'])

    alert_rows = []
    current_state = {
        'exit_recommendations': build_exit_recommendations(pd.DataFrame(base_positions), signals, partial_order),
        'broker_positions': pd.DataFrame(base_positions),
    }
    monkeypatch.setattr(execution, 'load_latest_csv', lambda prefix: current_state[prefix])
    monkeypatch.setattr(execution, 'notify_discord', lambda *args, **kwargs: alert_rows.append((args, kwargs)) or True)
    monkeypatch.setattr(execution, 'submit_single_order',
                        lambda *_args: (_ for _ in ()).throw(AssertionError('degraded spread close attempted')))
    config = TradingConfig(trading_mode='paper', auto_submit=True, discord_webhook_url='https://example.test/hook')

    preview, results = execution.run_exit_execution_cycle(config)

    assert preview.empty
    assert results.empty
    assert len(alert_rows) == 1
    assert 'MANUAL_REVIEW_REQUIRED' in capsys.readouterr().out

    current_state['exit_recommendations'] = apply_automatic_exit_guardrails(
        build_exit_recommendations(pd.DataFrame(base_positions), signals), enable_auto_approve=True,
    )
    current_state['broker_positions'] = pd.DataFrame([
        base_positions[0], {**base_positions[1], 'early_assignment_flag': True},
    ])
    preview, results = execution.run_exit_execution_cycle(config)
    assert preview.iloc[0]['spread_lifecycle_status'] == 'MANUAL_REVIEW_REQUIRED'
    assert bool(preview.iloc[0]['spread_alert_required'])
    assert not bool(preview.iloc[0]['ready_for_broker_submit'])
    assert results.empty
    assert len(alert_rows) == 2
    assert 'early assignment' in capsys.readouterr().out.lower()


def test_exit_recommendations_quarantine_orphaned_known_spread_leg() -> None:
    positions = pd.DataFrame([
        {'symbol': 'NVDA261218C00100000', 'side': 'long', 'qty': 1, 'market_value': 200.0, 'unrealized_pl': -20.0},
        {'symbol': 'AMD261218C00100000', 'side': 'long', 'qty': 1, 'market_value': 200.0, 'unrealized_pl': -20.0},
    ])
    signals = pd.DataFrame([
        {'ticker': 'NVDA', 'signal_score': -2, 'bias': 'bearish'},
        {'ticker': 'AMD', 'signal_score': -2, 'bias': 'bearish'},
    ])
    orders = pd.DataFrame([
        {'order_id': 'parent-nvda', 'client_order_id': 'paper-spread-NVDA-stable',
         'underlying_symbol': 'NVDA', 'order_class': 'mleg', 'status': 'partially_filled',
         'filled_qty': 1, 'legs_json': '[{"symbol":"NVDA261218C00100000"},{"symbol":"NVDA261218C00105000"}]',
         'spread_reconciliation_status': 'mismatched'},
        {'order_id': 'parent-amd', 'client_order_id': 'paper-spread-AMD-stable',
         'underlying_symbol': 'AMD', 'order_class': 'mleg', 'status': 'partially_filled',
         'filled_qty': 1, 'legs_json': '[]', 'spread_reconciliation_status': 'unknown'},
    ])

    recommendations = build_exit_recommendations(positions, signals, orders).set_index('symbol')
    for symbol in ('NVDA261218C00100000', 'AMD261218C00100000'):
        row = recommendations.loc[symbol]
        assert row['action'] == 'hold'
        assert row['spread_lifecycle_status'] == 'MANUAL_REVIEW_REQUIRED'
        assert row['exit_pct'] == 0
        assert not bool(row['exit_approved'])
        assert bool(row['spread_alert_required'])
    assert recommendations.loc['NVDA261218C00100000', 'spread_group_key'] == 'mleg:parent-nvda'
    assert recommendations.loc['AMD261218C00100000', 'paired_symbols'] == 'unknown legs'


def test_sync_broker_state_classifies_401_as_unauthorized(tmp_path: Path, monkeypatch) -> None:
    from src import execution

    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    monkeypatch.setattr(execution, 'OUTPUT_DIR', outputs)
    monkeypatch.setattr(execution, 'BROKER_AUTH_DIAGNOSTIC_FILE', outputs / 'broker_auth_diagnostic.json')

    config = TradingConfig(api_key='key', secret_key='secret', base_url='https://paper-api.alpaca.markets')

    class FakeResponse:
        status_code = 401

        def raise_for_status(self) -> None:
            raise requests.HTTPError('401 Client Error: Unauthorized for url: https://paper-api.alpaca.markets/v2/account', response=self)

        def json(self) -> dict[str, str]:
            return {}

    def fake_get(url, headers=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(execution.requests, 'get', fake_get)

    account, positions = execution.sync_broker_state(config)

    assert not account.empty
    assert str(account.iloc[0]['connection_status']) == 'unauthorized'
    assert 'Alpaca auth failed' in str(account.iloc[0]['detail'])
    assert positions.empty


def test_sync_broker_state_preserves_nested_spread_reconciliation(tmp_path: Path, monkeypatch) -> None:
    import src.execution as execution

    monkeypatch.setattr(execution, 'OUTPUT_DIR', tmp_path)
    (tmp_path / 'paper_trade_queue.csv').write_text('ticker,option_order_status\nNVDA,queued_for_review\n', encoding='utf-8')
    parent = {
        'id': 'parent-1', 'parent_order_id': '', 'symbol': 'NVDA', 'underlying_symbol': 'NVDA',
        'side': 'buy', 'qty': '2', 'status': 'partially_filled', 'submitted_at': '2026-11-12T10:00:00Z',
        'client_order_id': 'paper-spread-NVDA-test', 'order_class': 'mleg', 'filled_qty': '1',
        'filled_avg_price': '2.00',
        'legs': [
            {'id': 'leg-1', 'symbol': 'NVDA261218C00100000', 'ratio_qty': '1', 'status': 'partially_filled', 'filled_qty': '1'},
            {'id': 'leg-2', 'symbol': 'NVDA261218C00105000', 'ratio_qty': '1', 'status': 'partially_filled', 'filled_qty': '1'},
        ],
    }
    def fake_fetch(config, endpoint):
        if endpoint == '/v2/account':
            return {'status': 'ACTIVE', 'equity': '100000', 'cash': '50000', 'buying_power': '50000',
                    'portfolio_value': '100000', 'options_approved_level': 3, 'options_trading_level': 3,
                    'options_buying_power': '10000'}
        if endpoint == '/v2/clock':
            return {'is_open': False}
        if endpoint == '/v2/positions':
            return []
        if endpoint.startswith('/v2/orders'):
            assert 'nested=true' in endpoint
            return [parent]
        raise AssertionError(endpoint)

    monkeypatch.setattr(execution, '_fetch_alpaca_json', fake_fetch)
    config = TradingConfig(trading_mode='paper', api_key='paper', secret_key='paper')
    execution.sync_broker_state(config)
    orders = pd.read_csv(tmp_path / 'broker_orders.csv')
    assert orders.iloc[0]['order_id'] == 'parent-1'
    assert orders.iloc[0]['filled_qty'] == 1
    assert orders.iloc[0]['spread_reconciliation_status'] == 'matched'
    queue = pd.read_csv(tmp_path / 'paper_trade_queue.csv').iloc[0]
    assert queue['spread_order_id'] == 'parent-1'
    assert queue['spread_parent_status'] == 'partially_filled'
    assert queue['spread_reconciliation_status'] == 'matched'

    parent['legs'][1]['filled_qty'] = '0'
    execution.sync_broker_state(config)
    orders = pd.read_csv(tmp_path / 'broker_orders.csv')
    assert orders.iloc[0]['spread_reconciliation_status'] == 'mismatched'
    queue = pd.read_csv(tmp_path / 'paper_trade_queue.csv').iloc[0]
    assert queue['spread_reconciliation_status'] == 'mismatched'


def test_fetch_option_contracts_handles_401_without_raising(tmp_path: Path, monkeypatch) -> None:
    from src import execution

    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    monkeypatch.setattr(execution, 'OUTPUT_DIR', outputs)
    monkeypatch.setattr(execution, 'BROKER_AUTH_DIAGNOSTIC_FILE', outputs / 'broker_auth_diagnostic.json')

    config = TradingConfig(api_key='key', secret_key='secret', base_url='https://paper-api.alpaca.markets')

    class FakeResponse:
        status_code = 401

        def raise_for_status(self) -> None:
            raise requests.HTTPError(
                '401 Client Error: Unauthorized for url: https://paper-api.alpaca.markets/v2/options/contracts',
                response=self,
            )

        def json(self) -> dict[str, str]:
            return {}

    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(execution.requests, 'get', fake_get)

    contracts = execution.fetch_option_contracts(config, 'AMD', min_days=14, max_days=45)

    assert contracts.empty
    assert execution.BROKER_AUTH_DIAGNOSTIC_FILE.exists()
    detail = execution.BROKER_AUTH_DIAGNOSTIC_FILE.read_text(encoding='utf-8')
    assert '/v2/options/contracts' in detail


def test_build_performance_journal_creates_open_position_rows() -> None:
    positions = pd.DataFrame(
        [
            {'symbol': 'SNAP', 'side': 'long', 'qty': 100, 'market_value': 1000.0, 'unrealized_pl': 35.0},
            {'symbol': 'SNAP260424C00006000', 'side': 'long', 'qty': 2, 'market_value': 54.0, 'unrealized_pl': -4.0},
        ]
    )
    execution_log = pd.DataFrame(
        [
            {'submitted_at_utc': '2026-04-17T17:58:18+00:00', 'ticker': 'SNAP260424C00006000', 'underlying_ticker': 'SNAP', 'status': 'pending_new', 'execution_style': 'single_leg_option_paper'},
        ]
    )

    journal = build_performance_journal(positions, execution_log)

    assert len(journal) == 2
    assert set(journal['asset_class']) == {'stock', 'option'}
    assert 'open_winner' in set(journal['journal_status']) or 'open_loser' in set(journal['journal_status'])


def test_add_execution_status_to_candidates_marks_open_names() -> None:
    candidates = pd.DataFrame(
        [
            {'ticker': 'SNAP', 'bias': 'bullish', 'options_setup': 'Buy 6C', 'signal_score': 9},
            {'ticker': 'META', 'bias': 'bullish', 'options_setup': 'Buy 673C', 'signal_score': 8},
        ]
    )
    positions = pd.DataFrame(
        [
            {'symbol': 'SNAP260424C00006000', 'side': 'long', 'qty': 2, 'market_value': 54.0, 'unrealized_pl': -4.0},
        ]
    )
    execution_log = pd.DataFrame(
        [
            {'submitted_at_utc': '2026-04-17T17:58:18+00:00', 'ticker': 'SNAP260424C00006000', 'underlying_ticker': 'SNAP', 'status': 'pending_new', 'execution_style': 'single_leg_option_paper'},
        ]
    )

    marked = add_execution_status_to_candidates(candidates, positions, execution_log)

    snap_state = marked.loc[marked['ticker'] == 'SNAP', 'execution_state'].iloc[0]
    meta_state = marked.loc[marked['ticker'] == 'META', 'execution_state'].iloc[0]
    assert snap_state == 'Open / Executed'
    assert meta_state == 'Research Queue'
    assert bool(marked.loc[marked['ticker'] == 'SNAP', 'is_open_executed'].iloc[0]) is True


def test_queue_needs_recovery_flags_stale_hold_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(monitor, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setenv('PAPER_QUEUE_RECOVERY_MINUTES', '60')

    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    queue_path = tmp_path / 'paper_trade_queue.csv'
    pd.DataFrame([
        {
            'ticker': 'SNAP',
            'approval_status': 'hold',
            'queue_refreshed_at_utc': stale_ts,
        }
    ]).to_csv(queue_path, index=False)

    assert monitor._queue_needs_recovery(queue_path) is True


def test_build_priority_alerts_flags_urgent_positions() -> None:
    journal = pd.DataFrame(
        [
            {
                'symbol': 'IWM260420C00270000',
                'underlying_symbol': 'IWM',
                'asset_class': 'option',
                'unrealized_pl': -148.0,
                'market_value': 1226.0,
                'journal_status': 'open_loser',
            },
            {
                'symbol': 'QQQ',
                'underlying_symbol': 'QQQ',
                'asset_class': 'stock',
                'unrealized_pl': 53.0,
                'market_value': 3889.0,
                'journal_status': 'open_winner',
            },
        ]
    )
    exits = pd.DataFrame(
        [
            {'symbol': 'IWM260420C00270000', 'action': 'stop_out', 'priority': 1, 'days_to_expiration': 3, 'reason': 'Loss breached threshold.'}
        ]
    )

    alerts = build_priority_alerts(journal, exits)

    assert not alerts.empty
    assert 'IWM260420C00270000' in alerts['symbol'].tolist()
    assert 'urgent_risk' in alerts['alert_type'].tolist()
    assert alerts.loc[alerts['symbol'] == 'IWM260420C00270000', 'escalation_action'].iloc[0] == 'page_operator_now'


def test_apply_automatic_exit_guardrails_approves_urgent_rows() -> None:
    exits = pd.DataFrame(
        [
            {'symbol': 'IWM260420C00270000', 'asset_class': 'option', 'action': 'stop_out', 'priority': 1, 'days_to_expiration': 3, 'exit_approved': False, 'order_status': 'monitor'},
            {'symbol': 'QQQ', 'asset_class': 'stock', 'action': 'hold', 'priority': 9, 'days_to_expiration': '', 'exit_approved': False, 'order_status': 'monitor'},
        ]
    )

    updated = apply_automatic_exit_guardrails(exits, enable_auto_approve=True)

    assert bool(updated.loc[updated['symbol'] == 'IWM260420C00270000', 'exit_approved'].iloc[0]) is True
    assert updated.loc[updated['symbol'] == 'IWM260420C00270000', 'order_status'].iloc[0] == 'auto_approved'
    assert bool(updated.loc[updated['symbol'] == 'QQQ', 'exit_approved'].iloc[0]) is False


def test_build_exposure_summary_groups_by_underlying() -> None:
    positions = pd.DataFrame(
        [
            {'symbol': 'SNAP', 'market_value': 1200.0},
            {'symbol': 'SNAP260424C00006000', 'market_value': 54.0},
            {'symbol': 'QQQ', 'market_value': 3800.0},
        ]
    )

    summary = build_exposure_summary(positions, account_size_usd=100000.0)

    snap_row = summary.loc[summary['underlying_symbol'] == 'SNAP'].iloc[0]
    assert round(float(snap_row['total_market_value']), 2) == 1254.0
    assert int(snap_row['position_count']) == 2


def test_build_paper_trade_queue_applies_exposure_cap() -> None:
    candidates = pd.DataFrame(
        [
            {'ticker': 'META', 'options_setup': 'Buy 673C', 'signal_score': 9, 'projected_return_pct': 40.0, 'allocation_pct': 2.0, 'rank_score': 99.0, 'max_risk_usd': 300.0, 'bias': 'bullish'},
            {'ticker': 'NVDA', 'options_setup': 'Buy 198C', 'signal_score': 8, 'projected_return_pct': 35.0, 'allocation_pct': 2.0, 'rank_score': 98.0, 'max_risk_usd': 300.0, 'bias': 'bullish'},
        ]
    )
    positions = pd.DataFrame(
        [
            {'symbol': 'QQQ', 'market_value': 33000.0},
        ]
    )

    queue = build_paper_trade_queue(
        candidates,
        existing_tickers=set(),
        positions=positions,
        account_size_usd=100000.0,
        max_total_exposure_pct=35.0,
        max_positions=5,
    )

    assert list(queue['ticker']) == ['META']
    assert queue['guardrail_status'].iloc[0] == 'pass'


def test_build_paper_trade_queue_applies_sector_cap() -> None:
    candidates = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'options_setup': 'Buy 220C', 'signal_score': 9, 'projected_return_pct': 32.0, 'allocation_pct': 3.0, 'rank_score': 99.0, 'max_risk_usd': 250.0, 'bias': 'bullish'},
            {'ticker': 'XLF', 'options_setup': 'Buy 55C', 'signal_score': 8, 'projected_return_pct': 28.0, 'allocation_pct': 3.0, 'rank_score': 98.0, 'max_risk_usd': 250.0, 'bias': 'bullish'},
        ]
    )
    positions = pd.DataFrame(
        [
            {'symbol': 'QQQ', 'market_value': 16000.0},
        ]
    )

    queue = build_paper_trade_queue(
        candidates,
        existing_tickers=set(),
        positions=positions,
        account_size_usd=100000.0,
        max_total_exposure_pct=40.0,
        max_single_name_exposure_pct=20.0,
        max_queue_risk_usd=5000.0,
        max_sector_exposure_pct=15.0,
        max_positions=5,
    )

    assert list(queue['ticker']) == ['XLF']
    assert queue['sector'].iloc[0] == 'Financials'


def test_build_risk_overview_and_stress_table() -> None:
    positions = pd.DataFrame(
        [
            {'symbol': 'QQQ', 'market_value': 4000.0},
            {'symbol': 'SNAP260424C00006000', 'market_value': 600.0},
            {'symbol': 'XLF', 'market_value': 1400.0},
        ]
    )

    overview = build_risk_overview(positions, account_size_usd=100000.0)
    stress = build_stress_test_table(positions)

    assert not overview.empty
    assert round(float(overview.iloc[0]['total_exposure_pct']), 2) == 6.0
    assert 'largest_sector' in overview.columns
    assert not stress.empty
    assert {'scenario', 'shock_pct', 'estimated_pnl', 'stressed_market_value'}.issubset(stress.columns)
    assert float(stress.loc[stress['scenario'] == 'Risk-off -5%', 'estimated_pnl'].iloc[0]) < 0


def test_build_crypto_watchlist_flags_breakout_candidates() -> None:
    signals = pd.DataFrame(
        [
            {'ticker': 'BTC', 'bias': 'bullish', 'signal_score': 8, 'annualized_volatility': 0.55, 'advisor_summary': 'Momentum remains strong.'},
            {'ticker': 'ETH', 'bias': 'neutral', 'signal_score': 1, 'annualized_volatility': 0.42, 'advisor_summary': 'Mixed tape.'},
        ]
    )

    watchlist = build_crypto_watchlist(signals)

    assert not watchlist.empty
    assert 'crypto_action' in watchlist.columns
    assert watchlist.iloc[0]['ticker'] == 'BTC'
    assert watchlist.iloc[0]['asset_class'] == 'crypto'


def test_build_strategy_attribution_summarizes_wins_and_losses() -> None:
    journal = pd.DataFrame(
        [
            {'asset_class': 'stock', 'execution_style': 'equity_paper', 'unrealized_pl': 100.0, 'journal_status': 'open_winner', 'market_value': 1200.0},
            {'asset_class': 'option', 'execution_style': 'single_leg_option_paper', 'unrealized_pl': -40.0, 'journal_status': 'open_loser', 'market_value': 300.0},
        ]
    )

    attribution = build_strategy_attribution(journal)

    assert not attribution.empty
    assert {'strategy_bucket', 'positions', 'win_rate', 'total_unrealized_pl'}.issubset(attribution.columns)
    assert 'Equity Paper' in attribution['strategy_bucket'].tolist()


def test_build_execution_quality_report_tracks_fill_rate() -> None:
    execution_log = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'status': 'filled', 'execution_style': 'equity_paper'},
            {'ticker': 'MSFT', 'status': 'accepted', 'execution_style': 'equity_paper'},
            {'ticker': 'BTC', 'status': 'rejected', 'execution_style': 'crypto_paper'},
        ]
    )

    quality = build_execution_quality_report(execution_log)

    assert not quality.empty
    assert {'execution_style', 'submitted_orders', 'fill_rate_pct'}.issubset(quality.columns)
    assert float(quality.loc[quality['execution_style'] == 'Equity Paper', 'fill_rate_pct'].iloc[0]) == 50.0


def test_humanize_display_text_removes_separators() -> None:
    assert humanize_display_text('single_leg_option_paper') == 'Single Leg Option Paper'
    assert humanize_display_text('risk-off') == 'Risk Off'
    assert humanize_display_text('page_operator_now') == 'Page Operator Now'


def test_build_open_trade_timeline_sets_expected_close_dates() -> None:
    journal = pd.DataFrame(
        [
            {'symbol': 'AAPL', 'asset_class': 'stock', 'qty': 5, 'market_value': 950.0, 'unrealized_pl': -15.0},
            {'symbol': 'AMD260424C00277500', 'asset_class': 'option', 'qty': 1, 'market_value': 220.0, 'unrealized_pl': 18.0},
        ]
    )
    exits = pd.DataFrame(
        [
            {'symbol': 'AAPL', 'action': 'reduce_or_close', 'reason': 'The long thesis has weakened.', 'decision_window': 'Review at the next execution cycle.', 'close_or_exercise_plan': 'Close or reduce the stock position on the next execution cycle to protect capital.', 'expiration_date': '', 'days_to_expiration': ''},
            {'symbol': 'AMD260424C00277500', 'action': 'hold', 'reason': 'Signal remains intact.', 'decision_window': 'Review daily into expiry week (3 days left).', 'close_or_exercise_plan': 'Tighten the stop and review daily.', 'expiration_date': '04-24-2026', 'days_to_expiration': 3},
        ]
    )

    timeline = build_open_trade_timeline(journal, exits, reference_date='2026-04-17')

    assert not timeline.empty
    assert 'expected_close_date' in timeline.columns
    assert timeline.loc[timeline['symbol'] == 'AAPL', 'expected_close_date'].iloc[0] == '04-20-2026'
    assert timeline.loc[timeline['symbol'] == 'AMD260424C00277500', 'expected_close_date'].iloc[0] == '04-24-2026'


def test_build_options_candidates_applies_regime_aware_refinement(monkeypatch) -> None:
    from types import SimpleNamespace

    prices = {'AAPL': 210.0, 'MSFT': 420.0, 'QQQ': 510.0, 'TSLA': 175.0}
    monkeypatch.setattr('src.options_selector.stock_quote_midpoint', lambda client, ticker, age: prices[ticker])
    monkeypatch.setattr('src.options_selector.yf.Ticker', lambda ticker: SimpleNamespace(options=[]))
    signal_report = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'bias': 'bullish', 'suggested_structure': 'Long call', 'last_close': 210.0, 'signal_score': 7, 'annualized_volatility': 0.24, 'setup_quality': 6, 'long_rank_score': 92.0, 'short_rank_score': 8.0, 'advisor_summary': 'Strong upside trend.', 'sell_plan': 'Trim into strength.', 'opportunity_source': 'trend_breakout_scan', 'thesis_strength': 'high'},
            {'ticker': 'MSFT', 'bias': 'bullish', 'suggested_structure': 'Bull call spread', 'last_close': 420.0, 'signal_score': 6, 'annualized_volatility': 0.22, 'setup_quality': 5, 'long_rank_score': 88.0, 'short_rank_score': 10.0, 'advisor_summary': 'Momentum remains supportive.', 'sell_plan': 'Take partial profits into extensions.', 'opportunity_source': 'momentum_leaders', 'thesis_strength': 'high'},
            {'ticker': 'QQQ', 'bias': 'bullish', 'suggested_structure': 'Bull call spread', 'last_close': 510.0, 'signal_score': 5, 'annualized_volatility': 0.20, 'setup_quality': 5, 'long_rank_score': 81.0, 'short_rank_score': 12.0, 'advisor_summary': 'Breadth supports the move.', 'sell_plan': 'Respect a break of support.', 'opportunity_source': 'trend_breakout_scan', 'thesis_strength': 'medium'},
            {'ticker': 'TSLA', 'bias': 'bearish', 'suggested_structure': 'Long put', 'last_close': 175.0, 'signal_score': -6, 'annualized_volatility': 0.27, 'setup_quality': 5, 'long_rank_score': 12.0, 'short_rank_score': 78.0, 'advisor_summary': 'Weak relative trend.', 'sell_plan': 'Cover on momentum repair.', 'opportunity_source': 'breakdown_short_scan', 'thesis_strength': 'medium'},
        ]
    )

    candidates = build_options_candidates(signal_report, quote_client=object())

    bull = candidates.loc[candidates['ticker'] == 'AAPL'].iloc[0]
    bear = candidates.loc[candidates['ticker'] == 'TSLA'].iloc[0]

    assert 'base_rank_score' in candidates.columns
    assert 'refinement_adjustment' in candidates.columns
    assert 'regime_alignment' in candidates.columns
    assert 'refinement_note' in candidates.columns
    assert bull['regime_alignment'] == 'tailwind'
    assert bear['regime_alignment'] == 'headwind'
    assert float(bull['rank_score']) > float(bull['base_rank_score'])
    assert 'risk on' in str(bull['refinement_note']).lower()


def test_build_options_candidates_uses_recent_learning_feedback(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr('src.options_selector.stock_quote_midpoint', lambda *args: 210.0)
    monkeypatch.setattr('src.options_selector.yf.Ticker', lambda ticker: SimpleNamespace(options=[]))
    signal_report = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'bias': 'bullish', 'suggested_structure': 'Long call', 'last_close': 210.0, 'signal_score': 7, 'annualized_volatility': 0.24, 'setup_quality': 6, 'long_rank_score': 92.0, 'short_rank_score': 8.0, 'advisor_summary': 'Strong upside trend.', 'sell_plan': 'Trim into strength.', 'opportunity_source': 'trend_breakout_scan', 'thesis_strength': 'high'},
        ]
    )
    learning_feedback = pd.DataFrame(
        [
            {'ticker': 'AAPL', 'learning_adjustment': 4.0, 'learning_note': 'Recent paper trades in this name have been profitable.'},
        ]
    )

    candidates = build_options_candidates(signal_report, learning_feedback=learning_feedback, quote_client=object())

    row = candidates.iloc[0]
    assert 'learning_adjustment' in candidates.columns
    assert 'learning_note' in candidates.columns
    assert float(row['learning_adjustment']) == 4.0
    assert 'profitable' in str(row['learning_note']).lower()
    assert float(row['rank_score']) > float(row['base_rank_score'])


def test_build_market_regime_summary_identifies_risk_on_tape() -> None:
    signals = pd.DataFrame(
        [
            {'ticker': 'QQQ', 'bias': 'bullish', 'signal_score': 7, 'annualized_volatility': 0.20},
            {'ticker': 'SPY', 'bias': 'bullish', 'signal_score': 6, 'annualized_volatility': 0.18},
            {'ticker': 'IWM', 'bias': 'bullish', 'signal_score': 5, 'annualized_volatility': 0.22},
        ]
    )

    regime = build_market_regime_summary(signals)

    assert not regime.empty
    assert regime.iloc[0]['market_regime'] == 'risk_on'
    assert float(regime.iloc[0]['bullish_ratio']) == 1.0


# ─── Signal Engine: structure selection and score boundary tests ───────────────

def test_choose_structure_low_vol_bullish_returns_long_call() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(5, 0.20) == 'Long call'


def test_choose_structure_high_vol_bullish_returns_bull_spread() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(5, 0.30) == 'Bull call spread'


def test_choose_structure_low_vol_bearish_returns_long_put() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(-5, 0.20) == 'Long put'


def test_choose_structure_high_vol_bearish_returns_bear_spread() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(-5, 0.40) == 'Bear put spread'


def test_choose_structure_neutral_score_returns_no_trade() -> None:
    from src.signal_engine import choose_structure
    assert choose_structure(0, 0.30) == 'No trade / wait'
    assert choose_structure(2, 0.30) == 'No trade / wait'
    assert choose_structure(-2, 0.20) == 'No trade / wait'


def test_compute_rsi_overbought_series() -> None:
    from src.signal_engine import compute_rsi
    # RSI on a series with both gains and losses should produce finite values
    # Alternate: up 2 then down 1 — net bullish, so RSI should be above 50
    prices = []
    val = 100.0
    for i in range(60):
        val += 2.0 if i % 3 != 2 else -1.0
        prices.append(val)
    rsi = compute_rsi(pd.Series(prices))
    valid = rsi.dropna()
    assert len(valid) > 0
    assert float(valid.iloc[-1]) > 50


def test_compute_rsi_oversold_series() -> None:
    from src.signal_engine import compute_rsi
    # Alternate: down 2 then up 1 — net bearish, RSI should be below 50
    prices = []
    val = 100.0
    for i in range(60):
        val += -2.0 if i % 3 != 2 else 1.0
        prices.append(val)
    rsi = compute_rsi(pd.Series(prices))
    valid = rsi.dropna()
    assert len(valid) > 0
    assert float(valid.iloc[-1]) < 50


def test_signal_report_bullish_trending_stock() -> None:
    # Strong uptrend: monotonically rising prices
    prices = [float(80 + i * 0.8) for i in range(40)]
    market_data = {'TREND': _make_market_frame(prices)}
    report = build_signal_report(market_data)
    row = report.iloc[0]
    assert row['bias'] == 'bullish'
    assert int(row['signal_score']) > 0
    assert row['suggested_structure'] in {'Long call', 'Bull call spread'}


def test_signal_report_insufficient_data_returns_neutral() -> None:
    market_data = {'SPARSE': _make_market_frame([100.0, 101.0, 99.0])}
    report = build_signal_report(market_data)
    row = report.iloc[0]
    assert row['bias'] == 'insufficient_data'
    assert int(row['signal_score']) == 0


# ─── Exit rules: threshold boundary tests ─────────────────────────────────────

def test_exit_rules_stop_out_at_loss_threshold() -> None:
    from src.exit_manager import build_exit_recommendations
    positions = pd.DataFrame([{
        'symbol': 'SPY',
        'side': 'long',
        'qty': 4,
        'market_value': 2000.0,
        'unrealized_pl': -60.0,   # -3% — breaches -2% threshold
    }])
    signals = pd.DataFrame([{'ticker': 'SPY', 'signal_score': 7, 'bias': 'bullish'}])
    recs = build_exit_recommendations(positions, signals)
    assert recs.iloc[0]['action'] == 'stop_out'


def test_exit_rules_trim_winner_at_gain_threshold() -> None:
    from src.exit_manager import build_exit_recommendations
    positions = pd.DataFrame([{
        'symbol': 'QQQ',
        'side': 'long',
        'qty': 6,
        'market_value': 4000.0,
        'unrealized_pl': 200.0,   # +5% — exceeds +4% take-profit
    }])
    signals = pd.DataFrame([{'ticker': 'QQQ', 'signal_score': 9, 'bias': 'bullish'}])
    recs = build_exit_recommendations(positions, signals)
    assert recs.iloc[0]['action'] == 'trim_winner'


def test_exit_rules_hold_within_thresholds() -> None:
    from src.exit_manager import build_exit_recommendations
    positions = pd.DataFrame([{
        'symbol': 'MSFT',
        'side': 'long',
        'qty': 3,
        'market_value': 1200.0,
        'unrealized_pl': 6.0,    # +0.5% — within both thresholds
    }])
    signals = pd.DataFrame([{'ticker': 'MSFT', 'signal_score': 9, 'bias': 'bullish'}])
    recs = build_exit_recommendations(positions, signals)
    assert recs.iloc[0]['action'] == 'hold'


def test_exit_rules_reduce_on_signal_flip_to_neutral() -> None:
    from src.exit_manager import build_exit_recommendations
    positions = pd.DataFrame([{
        'symbol': 'AMD',
        'side': 'long',
        'qty': 4,
        'market_value': 1100.0,
        'unrealized_pl': -5.0,   # small loss but signal is now 0
    }])
    signals = pd.DataFrame([{'ticker': 'AMD', 'signal_score': 0, 'bias': 'neutral'}])
    recs = build_exit_recommendations(positions, signals)
    assert recs.iloc[0]['action'] in {'reduce_or_close', 'stop_out'}


def test_build_paper_trade_queue_applies_discovery_quality_hold(tmp_path: Path, monkeypatch) -> None:
    import paper_trade as pt

    wiki_dir = tmp_path / 'wiki'
    outputs_dir = tmp_path / 'outputs'
    wiki_dir.mkdir()
    outputs_dir.mkdir()

    payload = {
        'formula_version': 'v1',
        'threshold': 60,
        'items': [
            {
                'ticker': 'SNAP',
                'discovery_quality_score': 42.0,
                'status': 'block',
                'formula_version': 'v1',
                'meta': {'blocked_reasons': ['score below threshold 60']},
            }
        ],
    }
    (wiki_dir / 'discovery_quality.json').write_text(json.dumps(payload), encoding='utf-8')

    monkeypatch.setattr(pt, 'WIKI_DIR', wiki_dir)
    monkeypatch.setattr(pt, 'OUTPUT_DIR', outputs_dir)

    candidates = pd.DataFrame(
        [
            {
                'ticker': 'SNAP',
                'options_setup': 'Buy 6C',
                'signal_score': 8,
                'projected_return_pct': 35.0,
                'allocation_pct': 1.0,
                'rank_score': 95.0,
                'max_risk_usd': 300.0,
                'bias': 'bullish',
            }
        ]
    )

    queue = pt.build_paper_trade_queue(candidates, existing_tickers=set(), max_positions=5)

    assert not queue.empty
    row = queue.iloc[0]
    assert row['approval_status'] == 'hold'
    assert bool(row['approved_for_submit']) is False
    assert row['discovery_quality_status'] == 'block'
    assert 'Discovery quality gate blocked routing' in str(row['comment'])


def test_prepare_equity_execution_preview_holds_discovery_quality_blocks() -> None:
    queue = pd.DataFrame(
        [
            {
                'ticker': 'SNAP',
                'bias': 'bullish',
                'options_setup': 'Buy 6C',
                'signal_score': 8,
                'projected_return_pct': 42.0,
                'max_risk_usd': 200.0,
                'allocation_pct': 1.0,
                'approval_status': 'pending',
                'approved_for_submit': False,
                'order_status': 'queued_for_review',
                'discovery_quality_status': 'block',
            }
        ]
    )
    signals = pd.DataFrame([{'ticker': 'SNAP', 'last_close': 10.0}])

    config = TradingConfig(auto_submit=True, trading_mode='paper', approval_mode='automatic')
    preview = prepare_equity_execution_preview(queue, signals, config)

    assert not preview.empty
    row = preview.iloc[0]
    assert row['approval_status'] == 'hold'
    assert bool(row['approved_for_submit']) is False
    assert bool(row['ready_for_broker_submit']) is False


def test_prepare_equity_execution_preview_allows_discovery_quality_override() -> None:
    queue = pd.DataFrame(
        [
            {
                'ticker': 'SNAP',
                'bias': 'bullish',
                'options_setup': 'Buy 6C',
                'signal_score': 8,
                'projected_return_pct': 42.0,
                'max_risk_usd': 200.0,
                'allocation_pct': 1.0,
                'approval_status': 'pending',
                'approved_for_submit': False,
                'order_status': 'queued_for_review',
                'discovery_quality_status': 'block',
                'discovery_quality_override': True,
            }
        ]
    )
    signals = pd.DataFrame([{'ticker': 'SNAP', 'last_close': 10.0}])

    config = TradingConfig(auto_submit=True, trading_mode='paper', approval_mode='automatic')
    preview = prepare_equity_execution_preview(queue, signals, config)

    assert not preview.empty
    row = preview.iloc[0]
    assert row['approval_status'] == 'auto_approved'
    assert bool(row['approved_for_submit']) is True
    assert bool(row['ready_for_broker_submit']) is True


def test_append_note_adds_once_and_collapses_repeats() -> None:
    from paper_trade import _append_note

    note = 'Discovery quality gate blocked routing: score below threshold 60.'
    assert _append_note('Eligible.', note) == f'Eligible. {note}'
    assert _append_note(f'Eligible. {note} {note} {note}', note) == f'Eligible. {note}'
    assert _append_note('', note) == note


def test_build_paper_trade_queue_empty_existing_set_ignores_live_positions(monkeypatch, tmp_path: Path) -> None:
    import paper_trade as pt

    outputs_dir = tmp_path / 'outputs'
    outputs_dir.mkdir()
    pd.DataFrame([{'symbol': 'XLF261123C00058000', 'side': 'long', 'qty': 1, 'market_value': 90.0}]).to_csv(
        outputs_dir / 'broker_positions.csv', index=False
    )
    monkeypatch.setattr(pt, 'OUTPUT_DIR', outputs_dir)
    candidates = pd.DataFrame(
        [{'ticker': 'XLF', 'options_setup': 'Buy 55C', 'signal_score': 8, 'projected_return_pct': 28.0,
          'allocation_pct': 1.0, 'rank_score': 98.0, 'max_risk_usd': 250.0, 'bias': 'bullish'}]
    )

    queue = pt.build_paper_trade_queue(candidates, existing_tickers=set(), max_positions=5)

    assert list(queue['ticker']) == ['XLF']


def test_execution_quality_counts_real_fills_from_broker_orders() -> None:
    execution_log = pd.DataFrame(
        [{'ticker': 'XLF', 'status': 'pending_new', 'execution_style': 'single_leg_option_paper'}]
    )
    broker_orders = pd.DataFrame(
        [
            {'client_order_id': 'paper-option-XLF-1', 'status': 'filled'},
            {'client_order_id': 'paper-option-DIA-2', 'status': 'expired'},
            {'client_order_id': 'paper-exit-XLF-3', 'status': 'filled'},
        ]
    )

    quality = build_execution_quality_report(execution_log, broker_orders)
    option_row = quality.loc[quality['execution_style'] == 'Single Leg Option Paper'].iloc[0]
    exit_row = quality.loc[quality['execution_style'] == 'Protective Exit Paper'].iloc[0]

    assert int(option_row['filled_orders']) == 1
    assert float(option_row['fill_rate_pct']) == 50.0
    assert float(exit_row['fill_rate_pct']) == 100.0


def test_auth_gateway_rejects_login_when_credentials_not_configured(monkeypatch) -> None:
    import asyncio

    import pytest

    try:
        import auth_gateway
    except TypeError as exc:
        pytest.skip(f'installed fastapi/starlette versions are incompatible: {exc}')

    monkeypatch.setattr(auth_gateway, 'GATEWAY_EMAIL', '')
    monkeypatch.setattr(auth_gateway, 'GATEWAY_PASSWORD', '')

    response = asyncio.run(auth_gateway.login_submit(email='', password='', response=None))

    assert response.status_code == 401
