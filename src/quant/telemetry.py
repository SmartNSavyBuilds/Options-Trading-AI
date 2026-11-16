from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .contracts import ExecutionEvent, QuoteSnapshot, to_epoch_ms, utc_now_ms
from .db import connect, init_schema, quant_database_path


logger = logging.getLogger(__name__)
_EVENT_COLUMNS = tuple(ExecutionEvent.__dataclass_fields__)
_EVENT_TYPES = {'signal', 'submit', 'replace', 'fill', 'cancel', 'reject'}


def telemetry_enabled() -> bool:
    return os.getenv('QUANT_TELEMETRY_ENABLED', 'false').strip().lower() in {'1', 'true', 'yes', 'on'}


def record_event(event: ExecutionEvent, database=None) -> bool:
    if not telemetry_enabled() or event.event not in _EVENT_TYPES:
        return False
    connection = None
    try:
        target = database or quant_database_path()
        init_schema(target)
        connection = connect(target)
        row = event.to_row()
        columns = ', '.join(_EVENT_COLUMNS)
        placeholders = ', '.join('?' for _ in _EVENT_COLUMNS)
        values = tuple(row[column] for column in _EVENT_COLUMNS)
        with connection:
            connection.execute(
                f'INSERT OR IGNORE INTO execution_events ({columns}) VALUES ({placeholders})',
                values,
            )
        return True
    except Exception:
        logger.warning('Quant execution telemetry could not be recorded', exc_info=True)
        return False
    finally:
        if connection is not None:
            connection.close()


def record_spread_event(
    event: str,
    order: Mapping[str, Any] | Any,
    *,
    event_ts_ms: int | None = None,
    broker_order_id: str | None = None,
    ack_latency_ms: int | None = None,
    filled_qty: float | None = None,
    fill_px: float | None = None,
    quote_fetcher: Callable[[str], QuoteSnapshot | None] | None = None,
    quote_max_lag_ms: int | None = None,
    quality_flags: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
    database=None,
) -> bool:
    if not telemetry_enabled() or event not in _EVENT_TYPES:
        return False
    now_ms = utc_now_ms()
    observed_at = event_ts_ms if event_ts_ms is not None else now_ms
    get = order.get
    client_order_id = str(get('client_order_id', '') or '').strip()
    if not client_order_id:
        return False

    execution_style = str(get('execution_style', '') or '').lower()
    default_exit = 'protective_exit' in execution_style or 'close' in execution_style
    legs = []
    quote_by_symbol: dict[str, QuoteSnapshot] = {}
    flags = list(quality_flags)
    for role, symbol_key, intent_key, default_intent in (
        ('long', 'long_leg_symbol', 'long_leg_intent', 'sell_to_close' if default_exit else 'buy_to_open'),
        ('short', 'short_leg_symbol', 'short_leg_intent', 'buy_to_close' if default_exit else 'sell_to_open'),
    ):
        symbol = str(get(symbol_key, '') or '').strip().upper()
        if not symbol:
            continue
        intent = str(get(intent_key, default_intent) or default_intent).lower()
        quote = _cached_quote(symbol)
        if quote is not None and quote_max_lag_ms is not None:
            lag = abs(observed_at - quote.quote_ts_ms)
            if lag > quote_max_lag_ms:
                quote = None
                flags.append(f'{role}_quote_outside_fill_window')
        if quote is None and quote_fetcher is not None:
            try:
                quote = quote_fetcher(symbol)
            except Exception:
                flags.append(f'{role}_quote_unavailable')
            if quote is not None and quote_max_lag_ms is not None:
                if abs(observed_at - quote.quote_ts_ms) > quote_max_lag_ms:
                    quote = None
                    flags.append(f'{role}_quote_outside_fill_window')
        if quote is not None:
            quote_by_symbol[symbol] = quote
        else:
            flags.append(f'{role}_quote_unavailable')
        legs.append({
            'role': role,
            'symbol': symbol,
            'intent': intent,
            'bid': quote.bid if quote else None,
            'ask': quote.ask if quote else None,
            'mid': quote.mid if quote else None,
            'spread': quote.spread if quote else None,
            'spread_pct': quote.spread_pct if quote else None,
            'bid_size': quote.bid_size if quote else None,
            'ask_size': quote.ask_size if quote else None,
            'quote_ts_ms': quote.quote_ts_ms if quote else None,
            'captured_ts_ms': quote.captured_ts_ms if quote else None,
        })

    combo_bid = combo_ask = combo_mid = None
    if legs and len(quote_by_symbol) == len(legs):
        combo_bid = 0.0
        combo_ask = 0.0
        combo_mid = 0.0
        for leg in legs:
            quote = quote_by_symbol[leg['symbol']]
            is_buy = leg['intent'].startswith('buy')
            combo_bid += quote.bid if is_buy else -quote.ask
            combo_ask += quote.ask if is_buy else -quote.bid
            combo_mid += quote.mid if is_buy else -quote.mid

    quote_times = [quote.quote_ts_ms for quote in quote_by_symbol.values()]
    quote_lag = max((abs(observed_at - timestamp) for timestamp in quote_times), default=None)
    limit_px = _optional_number(get('limit_debit', get('limit_price')))
    fill_price = _optional_number(fill_px if fill_px is not None else get('filled_avg_price'))
    side = str(get('proxy_side', get('side', '')) or '').lower() or None
    slippage_bps = None
    if event == 'fill' and fill_price is not None and combo_mid not in (None, 0):
        signal_mid = _signal_mid(database or quant_database_path(), client_order_id)
        if signal_mid not in (None, 0):
            direction = -1 if side in {'sell', 'sell_to_close'} else 1
            slippage_bps = (fill_price - signal_mid) / abs(signal_mid) * 10000 * direction
        else:
            flags.append('signal_mid_unavailable')

    payload = dict(extra or {})
    if event == 'fill' and slippage_bps is not None:
        payload['slippage_reference'] = 'signal_time_mid'
    event_record = ExecutionEvent(
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        event=event,
        event_ts_ms=observed_at,
        structure=str(get('execution_style', '') or '') or None,
        side=side,
        qty=_optional_number(get('proxy_qty', get('qty'))),
        filled_qty=filled_qty if filled_qty is not None else _optional_number(get('filled_qty')),
        limit_px=limit_px,
        fill_px=fill_price,
        combo_bid=combo_bid,
        combo_ask=combo_ask,
        combo_mid=combo_mid,
        combo_spread=None if combo_bid is None or combo_ask is None else combo_ask - combo_bid,
        legs_json=json.dumps(legs, separators=(',', ':'), sort_keys=True),
        oldest_quote_ts_ms=min(quote_times) if quote_times else None,
        quote_lag_ms=quote_lag,
        ack_latency_ms=ack_latency_ms,
        slippage_bps_vs_signal_mid=slippage_bps,
        quality_flags=tuple(dict.fromkeys(flags)),
        extra_json=json.dumps(payload, separators=(',', ':'), sort_keys=True, default=str),
        recorded_ts_ms=now_ms,
    )
    return record_event(event_record, database)


def record_broker_fills(
    orders: Sequence[Mapping[str, Any]],
    config: Any,
    quote_fetcher: Callable[[str], QuoteSnapshot | None] | None = None,
    database=None,
) -> int:
    if not telemetry_enabled() or str(getattr(config, 'trading_mode', '')).lower() != 'paper':
        return 0
    recorded = 0
    try:
        target = database or quant_database_path()
        init_schema(target)
        for order in orders:
            if str(order.get('order_class', '')).lower() != 'mleg':
                continue
            status = str(order.get('status', '')).lower()
            if status not in {'filled', 'partially_filled'}:
                continue
            fill_timestamp = order.get('filled_at') or order.get('updated_at')
            if not fill_timestamp:
                continue
            fill_ts_ms = to_epoch_ms(fill_timestamp)
            client_order_id = str(order.get('client_order_id', '') or '')
            if not client_order_id or _event_exists(target, client_order_id, 'fill', fill_ts_ms):
                continue
            now_ms = utc_now_ms()
            max_lag_ms = int(os.getenv('QUANT_FILL_QUOTE_MAX_LAG_MS', '120000'))
            timestamp_inferred = not bool(order.get('filled_at'))
            fill_age = now_ms - fill_ts_ms
            flags = ['fill_timestamp_inferred'] if timestamp_inferred else []
            fetcher = quote_fetcher
            if fetcher is None and 0 <= fill_age <= max_lag_ms:
                fetcher = _alpaca_quote_fetcher(config)
            if fill_age < 0:
                flags.append('fill_timestamp_in_future')
            elif fill_age > max_lag_ms:
                flags.append('fill_quote_lookup_skipped_stale_fill')
            legs = order.get('legs') if isinstance(order.get('legs'), list) else []
            leg_symbols = [str(leg.get('symbol', '')).upper() for leg in legs if isinstance(leg, Mapping) and leg.get('symbol')]
            event_order = {
                'client_order_id': client_order_id,
                'broker_order_id': order.get('id'),
                'execution_style': 'multi_leg_option_paper',
                'proxy_side': order.get('side'),
                'proxy_qty': order.get('qty'),
                'filled_qty': order.get('filled_qty'),
                'filled_avg_price': order.get('filled_avg_price'),
                'limit_price': order.get('limit_price'),
                'long_leg_symbol': leg_symbols[0] if leg_symbols else '',
                'short_leg_symbol': leg_symbols[1] if len(leg_symbols) > 1 else '',
                'long_leg_intent': _broker_leg_intent(legs, 0),
                'short_leg_intent': _broker_leg_intent(legs, 1),
            }
            before = _event_exists(target, client_order_id, 'fill', fill_ts_ms)
            if before:
                continue
            if record_spread_event(
                'fill', event_order, event_ts_ms=fill_ts_ms,
                broker_order_id=str(order.get('id') or '') or None,
                filled_qty=_optional_number(order.get('filled_qty')),
                fill_px=_optional_number(order.get('filled_avg_price')),
                quote_fetcher=fetcher if 0 <= fill_age <= max_lag_ms else None,
                quote_max_lag_ms=max_lag_ms,
                quality_flags=flags,
                extra={'broker_status': status},
                database=target,
            ):
                recorded += 1
    except Exception:
        logger.warning('Quant broker fill telemetry could not be recorded', exc_info=True)
    return recorded


def _cached_quote(symbol: str) -> QuoteSnapshot | None:
    try:
        from ..market_data import LATEST_OPTION_QUOTES

        return LATEST_OPTION_QUOTES.get(symbol)
    except Exception:
        return None


def _alpaca_quote_fetcher(config: Any) -> Callable[[str], QuoteSnapshot | None] | None:
    try:
        from alpaca.data.historical import OptionHistoricalDataClient
        from ..market_data import LATEST_OPTION_QUOTES, option_snapshot_metrics

        client = OptionHistoricalDataClient(config.api_key, config.secret_key)

        def fetch(symbol: str) -> QuoteSnapshot | None:
            option_snapshot_metrics(client, symbol, float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60')))
            return LATEST_OPTION_QUOTES.get(symbol.upper())

        return fetch
    except Exception:
        logger.warning('Quant fill quote client is unavailable', exc_info=True)
        return None


def _event_exists(database, client_order_id: str, event: str, event_ts_ms: int) -> bool:
    connection = None
    try:
        connection = connect(database)
        row = connection.execute(
            'SELECT 1 FROM execution_events WHERE client_order_id=? AND event=? AND event_ts_ms=?',
            (client_order_id, event, event_ts_ms),
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _signal_mid(database, client_order_id: str) -> float | None:
    connection = None
    try:
        connection = connect(database)
        row = connection.execute(
            "SELECT combo_mid FROM execution_events WHERE client_order_id=? AND event='signal' "
            'AND combo_mid IS NOT NULL ORDER BY event_ts_ms LIMIT 1',
            (client_order_id,),
        ).fetchone()
        return None if row is None else float(row['combo_mid'])
    except Exception:
        return None
    finally:
        if connection is not None:
            connection.close()


def _broker_leg_intent(legs: Sequence[Any], index: int) -> str:
    if index >= len(legs) or not isinstance(legs[index], Mapping):
        return 'buy_to_open' if index == 0 else 'sell_to_open'
    leg = legs[index]
    side = str(leg.get('side', '')).lower()
    intent = str(leg.get('position_intent', '')).lower()
    if intent:
        if intent in {'buy_to_open', 'sell_to_open', 'buy_to_close', 'sell_to_close'}:
            return intent
        return f'{side}_{intent}' if side else intent
    return f'{side}_to_open' if side in {'buy', 'sell'} else ('buy_to_open' if index == 0 else 'sell_to_open')


def _optional_number(value: Any) -> float | None:
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None