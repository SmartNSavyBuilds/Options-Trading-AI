from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


_OCC_PATTERN = re.compile(r'^(?:O:)?([A-Z]{1,6})\s*(\d{6})([CP])(\d{8})$')
_CHAIN_FIELDS = (
    'underlying', 'expiry', 'strike', 'right', 'open', 'high', 'low', 'close', 'volume', 'vwap',
    'trade_count', 'open_interest', 'bid', 'ask', 'bid_size', 'ask_size', 'bid_exchange',
    'ask_exchange', 'quote_ts_ms', 'underlying_px', 'iv', 'delta', 'gamma', 'theta', 'vega',
    'rho', 'greeks_source',
)
_ALIASES = {
    'source': ('source', 'provider', 'vendor'),
    'interval': ('interval', 'timeframe', 'resolution'),
    'ts_ms': ('ts_ms', 'timestamp', 'ts', 'time', 'datetime', 'date', 'created'),
    'contract': ('contract', 'contract_symbol', 'option_symbol', 'symbol', 'ticker'),
    'underlying': ('underlying', 'underlying_symbol', 'underlying_ticker'),
    'expiry': ('expiry', 'expiration', 'expiration_date', 'expirationDate'),
    'strike': ('strike', 'strike_price', 'strikePrice'),
    'right': ('right', 'option_type', 'type', 'put_call'),
    'open': ('open', 'o'), 'high': ('high', 'h'), 'low': ('low', 'l'), 'close': ('close', 'c'),
    'volume': ('volume', 'v'), 'vwap': ('vwap', 'vw'),
    'trade_count': ('trade_count', 'transactions', 'n', 'count'),
    'open_interest': ('open_interest', 'openInterest', 'oi'),
    'bid': ('bid', 'bid_price', 'bidPrice'), 'ask': ('ask', 'ask_price', 'askPrice'),
    'bid_size': ('bid_size', 'bidSize', 'bidsize'), 'ask_size': ('ask_size', 'askSize', 'asksize'),
    'bid_exchange': ('bid_exchange', 'bidExchange'), 'ask_exchange': ('ask_exchange', 'askExchange'),
    'quote_ts_ms': ('quote_ts_ms', 'quote_timestamp', 'quoteTime', 'sip_timestamp'),
    'underlying_px': ('underlying_px', 'underlying_price', 'underlyingPrice'),
    'iv': ('iv', 'implied_vol', 'implied_volatility', 'impliedVolatility'),
    'delta': ('delta',), 'gamma': ('gamma',), 'theta': ('theta',), 'vega': ('vega',), 'rho': ('rho',),
    'greeks_source': ('greeks_source',),
}
_ALIAS_TO_FIELD = {alias: name for name, aliases in _ALIASES.items() for alias in aliases}


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def to_epoch_ms(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('Naive datetimes are not valid UTC timestamps')
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError('Timestamp must be finite')
        magnitude = abs(float(value))
        if magnitude < 1e11:
            return int(value * 1000)
        if magnitude < 1e14:
            return int(value)
        if magnitude < 1e17:
            return int(value / 1000)
        return int(value / 1_000_000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError('Timestamp is required')
        if re.fullmatch(r'-?\d+(?:\.\d+)?', text):
            return to_epoch_ms(float(text))
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return to_epoch_ms(parsed)
    raise TypeError(f'Unsupported timestamp type: {type(value).__name__}')


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    bid: float
    ask: float
    bid_size: float | None
    ask_size: float | None
    quote_ts_ms: int
    captured_ts_ms: int

    @property
    def mid(self) -> float | None:
        if not math.isfinite(self.bid) or not math.isfinite(self.ask) or self.bid <= 0 or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float | None:
        if self.mid is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        return None if mid is None else (self.ask - self.bid) / mid


@dataclass(frozen=True)
class ChainRecord:
    source: str
    interval: str
    ts_ms: int
    contract: str
    underlying: str | None = None
    expiry: str | None = None
    strike: float | None = None
    right: str | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    vwap: float | None = None
    trade_count: int | None = None
    open_interest: int | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    bid_exchange: str | None = None
    ask_exchange: str | None = None
    quote_ts_ms: int | None = None
    underlying_px: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    greeks_source: str | None = None
    quality_flags: tuple[str, ...] = ()
    extra_json: str = '{}'

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        source: str | None = None,
        interval: str | None = None,
    ) -> ChainRecord:
        normalized_payload = dict(payload)
        for section in ('day', 'greeks', 'quote', 'details', 'underlying_asset'):
            nested = payload.get(section)
            if not isinstance(nested, Mapping):
                continue
            for key, value in nested.items():
                nested_key = str(key)
                if section == 'quote' and nested_key in {'timestamp', 'time'}:
                    normalized_payload.setdefault('quote_timestamp', value)
                elif section == 'underlying_asset' and nested_key in {'ticker', 'symbol'}:
                    normalized_payload.setdefault('underlying', value)
                elif section == 'details' and nested_key in {'ticker', 'symbol'}:
                    normalized_payload.setdefault('contract_symbol', value)
                else:
                    normalized_payload.setdefault(nested_key, value)
        mapped: dict[str, Any] = {}
        consumed: set[str] = set()
        for key, value in normalized_payload.items():
            canonical = _ALIAS_TO_FIELD.get(str(key))
            if canonical:
                mapped.setdefault(canonical, value)
                consumed.add(str(key))
        resolved_source = str(source or mapped.get('source') or '').strip()
        resolved_interval = str(interval or mapped.get('interval') or 'eod').strip().lower()
        contract = str(mapped.get('contract') or '').strip().upper()
        if not resolved_source or not contract:
            raise ValueError('A source and option contract symbol are required')
        if resolved_interval not in {'eod', '1m', 'tick'}:
            raise ValueError('Interval must be eod, 1m, or tick')
        ts_ms = to_epoch_ms(mapped.get('ts_ms'))
        contract_match = _OCC_PATTERN.fullmatch(contract)
        parsed: dict[str, Any] = {}
        if contract_match:
            parsed = {
                'underlying': contract_match.group(1),
                'expiry': datetime.strptime(contract_match.group(2), '%y%m%d').date().isoformat(),
                'right': contract_match.group(3),
                'strike': int(contract_match.group(4)) / 1000,
            }
        raw_right = str(mapped.get('right') or parsed.get('right') or '').strip().upper()
        if raw_right in {'CALL', 'C'}:
            right = 'C'
        elif raw_right in {'PUT', 'P'}:
            right = 'P'
        else:
            right = None
        values: dict[str, Any] = {}
        flags: list[str] = []
        for name in _CHAIN_FIELDS:
            value = mapped.get(name, parsed.get(name))
            if name in {'underlying', 'bid_exchange', 'ask_exchange', 'greeks_source'}:
                values[name] = None if value is None else str(value)
            elif name == 'expiry':
                values[name] = None if value is None else str(value)[:10]
            elif name == 'right':
                values[name] = right
            elif name == 'quote_ts_ms':
                values[name] = None if value in (None, '') else to_epoch_ms(value)
            elif name in {'volume', 'trade_count', 'open_interest'}:
                values[name] = _optional_int(value)
            else:
                values[name] = _optional_float(value)
        if values['bid'] is None or values['ask'] is None:
            flags.append('missing_quote')
        elif values['bid'] <= 0 or values['ask'] <= 0:
            flags.append('nonpositive_quote')
        elif values['ask'] < values['bid']:
            flags.append('crossed_quote')
        if values['iv'] is not None and not 0 < values['iv'] <= 5:
            flags.append('iv_out_of_range')
        extra = {key: value for key, value in payload.items() if str(key) not in consumed}
        bid = values['bid']
        ask = values['ask']
        if bid is not None and ask is not None and bid > 0 and ask >= bid:
            values['mid'] = (bid + ask) / 2
            values['spread'] = ask - bid
            values['spread_pct'] = (ask - bid) / values['mid'] if values['mid'] > 0 else None
        else:
            values['mid'] = values['spread'] = values['spread_pct'] = None
        return cls(
            source=resolved_source,
            interval=resolved_interval,
            ts_ms=ts_ms,
            contract=contract,
            quality_flags=tuple(flags),
            extra_json=json.dumps(extra, sort_keys=True, separators=(',', ':'), default=str),
            **values,
        )

    def to_row(self, ingested_ts_ms: int | None = None) -> dict[str, Any]:
        row = asdict(self)
        row['quality_flags'] = json.dumps(self.quality_flags, separators=(',', ':'))
        row['ingested_ts_ms'] = ingested_ts_ms if ingested_ts_ms is not None else utc_now_ms()
        return row


@dataclass(frozen=True)
class ExecutionEvent:
    client_order_id: str
    event: str
    event_ts_ms: int
    broker_order_id: str | None = None
    structure: str | None = None
    side: str | None = None
    qty: float | None = None
    filled_qty: float | None = None
    limit_px: float | None = None
    fill_px: float | None = None
    combo_bid: float | None = None
    combo_ask: float | None = None
    combo_mid: float | None = None
    combo_spread: float | None = None
    legs_json: str = '[]'
    oldest_quote_ts_ms: int | None = None
    quote_lag_ms: int | None = None
    ack_latency_ms: int | None = None
    slippage_bps_vs_signal_mid: float | None = None
    quality_flags: tuple[str, ...] = ()
    extra_json: str = '{}'
    recorded_ts_ms: int = field(default_factory=utc_now_ms)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row['quality_flags'] = json.dumps(self.quality_flags, separators=(',', ':'))
        return row


def _optional_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    number = _optional_float(value)
    return None if number is None else int(number)