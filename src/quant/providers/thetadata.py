from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from ..pricing import american_implied_vol
from .base import ChainPage


_NEW_YORK = ZoneInfo('America/New_York')
_PERMISSION_STATUS_CODES = {401, 403, 471}
_PERMISSION_MARKERS = ('permission', 'subscription', 'not authorized', 'unauthorized', 'upgrade')
LOCAL_IV_SOURCE = 'local_american_crr_nbbo_mid'


class ThetaDataPermissionError(RuntimeError):
    pass


class ThetaDataProvider:
    name = 'thetadata'

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        session=None,
        free_tier: bool | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv('THETADATA_BASE_URL', 'http://127.0.0.1:25503/v3')).rstrip('/')
        self.timeout_seconds = float(timeout_seconds or os.getenv('THETADATA_TIMEOUT_SECONDS', '60'))
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError('ThetaData timeout must be a positive finite number')
        self.session = session or requests.Session()
        if free_tier is None:
            free_tier = os.getenv('THETADATA_FREE_TIER', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
        self.greeks_available = not free_tier
        self.risk_free_rate = _env_float('QUANT_RISK_FREE_RATE', 0.04)
        self.dividend_yield = _env_float('QUANT_DIVIDEND_YIELD', 0.0)
        self.local_iv_max_dte = int(_env_float('THETADATA_LOCAL_IV_MAX_DTE', 120))
        self.local_iv_max_moneyness = _env_float('THETADATA_LOCAL_IV_MAX_MONEYNESS', 0.25)
        self.local_iv_steps = int(_env_float('THETADATA_LOCAL_IV_STEPS', 150))
        if self.local_iv_steps < 10 or self.local_iv_max_dte <= 0 or self.local_iv_max_moneyness <= 0:
            raise ValueError('Local IV settings must be positive and use at least 10 tree steps')

    def fetch_chain_page(
        self,
        underlying: str,
        start_date: date,
        end_date: date,
        interval: str,
        cursor: str | None,
    ) -> ChainPage:
        if interval != 'eod':
            raise ValueError('ThetaDataProvider currently supports EOD history only')
        day = date.fromisoformat(cursor) if cursor else start_date
        while day <= end_date and day.weekday() >= 5:
            day += timedelta(days=1)
        if day > end_date:
            return ChainPage((), None)

        day_param = day.strftime('%Y%m%d')
        base_params = {
            'symbol': underlying.strip().upper(),
            'expiration': '*',
            'start_date': day_param,
            'end_date': day_param,
            'format': 'json',
        }
        eod_rows = self._get_rows('/option/history/eod', base_params)
        greek_rows: list[dict[str, Any]] = []
        if self.greeks_available:
            try:
                greek_rows = self._get_rows(
                    '/option/history/greeks/eod',
                    {**base_params, 'option_use_nbbo': 'true', 'underlyer_use_nbbo': 'true'},
                )
            except ThetaDataPermissionError:
                self.greeks_available = False
        spot = None if self.greeks_available or not eod_rows else self._underlying_spot(underlying, day_param)

        greek_by_contract = {_contract_key(row): row for row in greek_rows if _contract_key(row)}
        eod_by_contract = {_contract_key(row): row for row in eod_rows if _contract_key(row)}
        normalized_records = []
        for key in dict.fromkeys((*eod_by_contract.keys(), *greek_by_contract.keys())):
            greeks = greek_by_contract.get(key)
            merged = {**eod_by_contract.get(key, {}), **(greeks or {})}
            contract = _occ_symbol(underlying, merged)
            if not contract:
                continue
            timestamp = merged.get('timestamp') or merged.get('created')
            timestamp = _normalize_theta_timestamp(timestamp, day)
            merged.pop('symbol', None)
            merged.pop('created', None)
            merged['contract'] = contract
            merged['underlying'] = underlying.strip().upper()
            merged['timestamp'] = timestamp.isoformat(timespec='milliseconds')
            merged['quote_timestamp'] = timestamp.isoformat(timespec='milliseconds')
            merged['provider'] = self.name
            merged['interval'] = 'eod'
            if greeks:
                merged['greeks_source'] = 'thetadata_eod_nbbo'
            else:
                self._apply_local_iv(merged, key, spot, day)
            normalized_records.append(merged)

        following_day = day + timedelta(days=1)
        next_cursor = following_day.isoformat() if following_day <= end_date else None
        return ChainPage(normalized_records, next_cursor)

    def _underlying_spot(self, underlying: str, day_param: str) -> float | None:
        try:
            rows = self._get_rows('/stock/history/eod', {
                'symbol': underlying.strip().upper(), 'start_date': day_param,
                'end_date': day_param, 'format': 'json',
            })
        except ThetaDataPermissionError:
            return None
        for row in rows:
            bid, ask, close = (_positive(row.get(name)) for name in ('bid', 'ask', 'close'))
            if bid is not None and ask is not None and ask >= bid:
                return (bid + ask) / 2
            if close is not None:
                return close
        return None

    def _apply_local_iv(
        self,
        merged: dict[str, Any],
        key: tuple[str, str, float, str],
        spot: float | None,
        observed_date: date,
    ) -> None:
        _, expiry_text, strike, right = key
        merged['greeks_source'] = LOCAL_IV_SOURCE
        merged['local_iv_rate'] = self.risk_free_rate
        merged['local_iv_dividend_yield'] = self.dividend_yield
        bid, ask = _positive(merged.get('bid')), _positive(merged.get('ask'))
        status = 'ok'
        try:
            dte = (date.fromisoformat(expiry_text) - observed_date).days
        except ValueError:
            dte = 0
        if spot is None:
            status = 'missing_underlying_spot'
        elif bid is None or ask is None or ask < bid:
            status = 'invalid_option_quote'
        elif dte <= 0:
            status = 'expired_or_same_day'
        elif dte > self.local_iv_max_dte or abs(strike - spot) / spot > self.local_iv_max_moneyness:
            status = 'outside_local_iv_scope'
        else:
            implied = american_implied_vol(
                (bid + ask) / 2, spot, strike, dte / 365, self.risk_free_rate,
                self.dividend_yield, right, self.local_iv_steps,
            )
            if implied is None:
                status = 'no_arbitrage_free_solution'
            else:
                merged['implied_vol'] = implied
        if spot is not None:
            merged['underlying_price'] = spot
        merged['local_iv_status'] = status

    def _get_rows(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        response = self.session.get(
            f'{self.base_url}{path}', params=params, timeout=self.timeout_seconds,
        )
        status_code = getattr(response, 'status_code', 200)
        if status_code in _PERMISSION_STATUS_CODES:
            raise ThetaDataPermissionError(f'ThetaData permission denied for {path} (HTTP {status_code})')
        if status_code == 472:
            return []
        response.raise_for_status()
        body = response.json()
        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict) and isinstance(body.get('data'), list):
            rows = body['data']
        elif isinstance(body, dict) and body.get('error'):
            message = str(body['error'])
            if any(marker in message.lower() for marker in _PERMISSION_MARKERS):
                raise ThetaDataPermissionError(f'ThetaData permission denied for {path}: {message}')
            raise RuntimeError(f'ThetaData API error: {message}')
        elif body is None:
            rows = []
        else:
            raise ValueError(f'Unexpected ThetaData response for {path}')
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f'ThetaData response contains a non-object row for {path}')
        return rows


def _contract_key(row: dict[str, Any]) -> tuple[str, str, float, str] | None:
    try:
        expiry = str(row.get('expiration') or row.get('expiry'))[:10]
        strike = float(row['strike'])
        right = _normalize_right(row.get('right'))
        if not expiry or not math.isfinite(strike) or strike <= 0 or right is None:
            return None
        return str(row.get('symbol', '')).strip().upper(), expiry, strike, right
    except (KeyError, TypeError, ValueError):
        return None


def _occ_symbol(underlying: str, row: dict[str, Any]) -> str | None:
    key = _contract_key(row)
    if key is None:
        return None
    root, expiry, strike, right = key
    if root and root != underlying.strip().upper():
        return None
    try:
        expiry_date = date.fromisoformat(expiry)
    except ValueError:
        return None
    strike_millis = round(strike * 1000)
    if abs(strike * 1000 - strike_millis) > 1e-6 or strike_millis > 99_999_999:
        return None
    return f'{underlying.strip().upper()}{expiry_date:%y%m%d}{right}{strike_millis:08d}'


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _env_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


def _normalize_right(value: Any) -> str | None:
    right = str(value or '').strip().lower()
    if right in {'c', 'call'}:
        return 'C'
    if right in {'p', 'put'}:
        return 'P'
    return None


def _normalize_theta_timestamp(value: Any, observed_date: date) -> datetime:
    if value:
        text = str(value).strip()
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_NEW_YORK)
        return parsed
    return datetime.combine(observed_date, time(17, 15), tzinfo=_NEW_YORK)