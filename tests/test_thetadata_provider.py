from __future__ import annotations

from datetime import date

import pytest

from src.quant.contracts import ChainRecord
from src.quant.pricing import american_implied_vol, american_price
from src.quant.providers.thetadata import LOCAL_IV_SOURCE, ThetaDataProvider


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        response = self.responses.pop(0)
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


def test_thetadata_eod_page_requests_documented_endpoints_and_merges_greeks() -> None:
    session = FakeSession([
        [{
            'symbol': 'SPY', 'expiration': '2026-12-18', 'strike': 100, 'right': 'call',
            'created': '2026-11-16T17:15:00.000', 'open': 1.0, 'high': 1.2, 'low': 0.9,
            'close': 1.1, 'volume': 45, 'count': 8, 'bid': 1.05, 'ask': 1.15,
            'bid_size': 10, 'ask_size': 12,
        }],
        [{
            'symbol': 'SPY', 'expiration': '2026-12-18', 'strike': 100, 'right': 'call',
            'timestamp': '2026-11-16T17:15:00.000', 'implied_vol': 0.24, 'delta': 0.51,
            'gamma': 0.03, 'theta': -0.02, 'vega': 0.1, 'rho': 0.01, 'underlying_price': 100.2,
        }],
    ])
    provider = ThetaDataProvider(session=session, timeout_seconds=12)

    page = provider.fetch_chain_page('SPY', date(2026, 11, 16), date(2026, 11, 16), 'eod', None)

    assert page.next_cursor is None
    assert len(page.records) == 1
    record = page.records[0]
    assert record['contract'] == 'SPY261218C00100000'
    assert record['underlying'] == 'SPY'
    assert record['open'] == 1.0 and record['close'] == 1.1 and record['volume'] == 45
    assert record['bid'] == 1.05 and record['ask'] == 1.15
    assert record['implied_vol'] == 0.24 and record['delta'] == 0.51
    assert record['underlying_price'] == 100.2
    normalized = ChainRecord.from_mapping(record)
    assert normalized.iv == 0.24 and normalized.underlying_px == 100.2
    assert normalized.ts_ms > 0 and normalized.quote_ts_ms == normalized.ts_ms
    assert len(session.calls) == 2
    eod_url, eod_params, eod_timeout = session.calls[0]
    greeks_url, greeks_params, _ = session.calls[1]
    assert eod_url.endswith('/option/history/eod')
    assert greeks_url.endswith('/option/history/greeks/eod')
    assert eod_params['expiration'] == '*' and eod_params['format'] == 'json'
    assert eod_params['start_date'] == eod_params['end_date'] == '20261116'
    assert greeks_params['option_use_nbbo'] == 'true'
    assert greeks_params['underlyer_use_nbbo'] == 'true'
    assert eod_timeout == 12


def test_thetadata_page_cursor_advances_day_and_skips_weekends() -> None:
    session = FakeSession([[], [], [], [], [], []])
    provider = ThetaDataProvider(session=session)

    friday = provider.fetch_chain_page('SPY', date(2026, 11, 13), date(2026, 11, 17), 'eod', None)
    monday = provider.fetch_chain_page('SPY', date(2026, 11, 13), date(2026, 11, 17), 'eod', friday.next_cursor)
    tuesday = provider.fetch_chain_page('SPY', date(2026, 11, 13), date(2026, 11, 17), 'eod', monday.next_cursor)

    assert friday.next_cursor == '2026-11-14'
    assert monday.next_cursor == '2026-11-17'
    assert tuesday.next_cursor is None
    assert len(session.calls) == 6
    assert session.calls[0][1]['start_date'] == '20261113'
    assert session.calls[2][1]['start_date'] == '20261116'


def test_thetadata_rejects_non_eod_interval_and_api_errors() -> None:
    provider = ThetaDataProvider(session=FakeSession([{'error': 'subscription required'}]))
    with pytest.raises(ValueError, match='EOD'):
        provider.fetch_chain_page('SPY', date(2026, 11, 16), date(2026, 11, 16), '1m', None)
    with pytest.raises(RuntimeError, match='subscription required'):
        provider.fetch_chain_page('SPY', date(2026, 11, 16), date(2026, 11, 16), 'eod', None)


def _put_eod_row(price: float, expiration: str = '2026-12-16', strike: float = 100) -> dict:
    return {
        'symbol': 'SPY', 'expiration': expiration, 'strike': strike, 'right': 'put',
        'created': '2026-11-16T17:15:00.000', 'bid': round(price - 0.05, 4), 'ask': round(price + 0.05, 4),
        'close': price, 'volume': 10,
    }


_STOCK_EOD = [{'created': '2026-11-16T17:15:00.000', 'bid': 99.9, 'ask': 100.1, 'close': 100.05}]


@pytest.fixture
def default_local_iv_settings(monkeypatch):
    for name in ('THETADATA_FREE_TIER', 'QUANT_RISK_FREE_RATE', 'QUANT_DIVIDEND_YIELD',
                 'THETADATA_LOCAL_IV_MAX_DTE', 'THETADATA_LOCAL_IV_MAX_MONEYNESS', 'THETADATA_LOCAL_IV_STEPS'):
        monkeypatch.delenv(name, raising=False)


def test_free_tier_flag_skips_greeks_and_solves_iv_locally(default_local_iv_settings, monkeypatch) -> None:
    monkeypatch.setenv('THETADATA_FREE_TIER', 'true')
    price = american_price(100, 100, 30 / 365, 0.04, 0.0, 0.3, 'P')
    session = FakeSession([[_put_eod_row(price)], _STOCK_EOD])
    provider = ThetaDataProvider(session=session)

    page = provider.fetch_chain_page('SPY', date(2026, 11, 16), date(2026, 11, 16), 'eod', None)

    assert [call[0].rsplit('/v3', 1)[1] for call in session.calls] == ['/option/history/eod', '/stock/history/eod']
    record = page.records[0]
    assert record['greeks_source'] == LOCAL_IV_SOURCE
    assert record['local_iv_status'] == 'ok'
    assert record['underlying_price'] == pytest.approx(100.0)
    assert record['implied_vol'] == pytest.approx(0.3, abs=1e-4)
    normalized = ChainRecord.from_mapping(record)
    assert normalized.iv == pytest.approx(0.3, abs=1e-4)
    assert normalized.delta is None and normalized.greeks_source == LOCAL_IV_SOURCE


def test_permission_error_on_greeks_falls_back_and_stops_retrying(default_local_iv_settings) -> None:
    price = american_price(100, 100, 30 / 365, 0.04, 0.0, 0.3, 'P')
    session = FakeSession([
        [_put_eod_row(price)], FakeResponse({}, status_code=471), _STOCK_EOD,
        [],
    ])
    provider = ThetaDataProvider(session=session)

    first = provider.fetch_chain_page('SPY', date(2026, 11, 16), date(2026, 11, 17), 'eod', None)
    provider.fetch_chain_page('SPY', date(2026, 11, 16), date(2026, 11, 17), 'eod', first.next_cursor)

    paths = [call[0].rsplit('/v3', 1)[1] for call in session.calls]
    assert paths == ['/option/history/eod', '/option/history/greeks/eod', '/stock/history/eod', '/option/history/eod']
    assert provider.greeks_available is False
    assert first.records[0]['implied_vol'] == pytest.approx(0.3, abs=1e-4)


def test_local_iv_records_status_when_unsolvable_or_out_of_scope(default_local_iv_settings) -> None:
    session = FakeSession([
        [_put_eod_row(5.0, strike=110), _put_eod_row(1.0, expiration='2027-11-19')],
        _STOCK_EOD,
    ])
    provider = ThetaDataProvider(session=session, free_tier=True)

    page = provider.fetch_chain_page('SPY', date(2026, 11, 16), date(2026, 11, 16), 'eod', None)

    statuses = {record['contract']: record['local_iv_status'] for record in page.records}
    assert statuses == {
        'SPY261216P00110000': 'no_arbitrage_free_solution',
        'SPY271119P00100000': 'outside_local_iv_scope',
    }
    assert all('implied_vol' not in record for record in page.records)


def test_american_solver_matches_black_scholes_call_and_values_early_exercise() -> None:
    import math

    from scipy.stats import norm

    spot, strike, years, rate, sigma = 100.0, 100.0, 30 / 365, 0.04, 0.25
    d1 = (math.log(spot / strike) + (rate + sigma ** 2 / 2) * years) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    european_call = spot * norm.cdf(d1) - strike * math.exp(-rate * years) * norm.cdf(d2)
    european_put = strike * math.exp(-rate * years) * norm.cdf(-d2) - spot * norm.cdf(-d1)

    assert american_price(spot, strike, years, rate, 0.0, sigma, 'C', 400) == pytest.approx(european_call, abs=0.01)
    assert american_price(spot, strike, years, rate, 0.0, sigma, 'P', 400) >= european_put
    assert american_implied_vol(0.5, 100, 110, years, rate, 0.0, 'P') is None
    assert american_implied_vol(101, 100, 100, years, rate, 0.0, 'C') is None