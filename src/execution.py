from __future__ import annotations

import json
import hashlib
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any

import pandas as pd
import requests


PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)

PAPER_BASE_URL = 'https://paper-api.alpaca.markets'
LIVE_BASE_URL = 'https://api.alpaca.markets'
BROKER_AUTH_DIAGNOSTIC_FILE = OUTPUT_DIR / 'broker_auth_diagnostic.json'

_TRANSIENT_NETWORK_MARKERS = (
    'temporary failure in name resolution',
    'name resolution',
    'nodename nor servname provided',
    'failed to establish a new connection',
    'connection reset',
    'timed out',
    'dns',
)

_ACTIVE_SUBMISSION_STATUSES = {
    'new',
    'accepted',
    'partially_filled',
    'submitted',
    'pending_new',
}


def _load_env_file() -> None:
    env_path = PROJECT_DIR / '.env'
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _save_broker_auth_diagnostic(detail: dict[str, Any]) -> None:
    try:
        BROKER_AUTH_DIAGNOSTIC_FILE.write_text(json.dumps(detail, indent=2, sort_keys=True), encoding='utf-8')
    except Exception:
        pass


def _describe_broker_auth_failure(config: 'TradingConfig', endpoint: str, exc: Exception) -> str:
    status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
    lowered = str(exc).lower()
    is_auth_failure = status_code == 401 or 'unauthorized' in lowered or 'invalid api key' in lowered or 'authentication failed' in lowered
    if not is_auth_failure:
        return str(exc)

    diagnosis = {
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'broker': config.broker,
        'mode': config.trading_mode,
        'base_url': config.base_url,
        'endpoint': endpoint,
        'status_code': status_code,
        'api_key_present': bool(config.api_key),
        'secret_key_present': bool(config.secret_key),
        'detail': str(exc),
        'likely_causes': [
            'ALPACA_API_KEY or ALPACA_SECRET_KEY is missing, revoked, or mismatched',
            'paper and live credentials are mixed with the wrong base URL',
            'the Alpaca account does not have permission for the requested endpoint',
        ],
    }
    _save_broker_auth_diagnostic(diagnosis)
    return (
        f'Alpaca auth failed with HTTP {status_code or 401} at {endpoint}. '
        'Check that ALPACA_API_KEY and ALPACA_SECRET_KEY match the selected paper/live base URL, '
        'and verify the credentials have not been revoked.'
    )


def _request_max_attempts() -> int:
    raw = str(os.getenv('ALPACA_REQUEST_MAX_ATTEMPTS', '3') or '3').strip()
    try:
        return max(1, min(int(raw), 6))
    except ValueError:
        return 3


def _is_transient_network_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    lowered = str(exc).lower()
    return any(marker in lowered for marker in _TRANSIENT_NETWORK_MARKERS)


def _request_with_retries(
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: int,
    json_body: dict[str, Any] | None = None,
) -> requests.Response:
    attempts = _request_max_attempts()
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method=method, url=url, headers=headers, json=json_body, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
            retryable_http = status_code in {429, 500, 502, 503, 504}
            if retryable_http and attempt < attempts:
                time.sleep(min(6.0, 0.75 * attempt))
                last_exc = exc
                continue
            raise
        except Exception as exc:
            if attempt < attempts and _is_transient_network_error(exc):
                time.sleep(min(6.0, 0.75 * attempt))
                last_exc = exc
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError('Request retries exhausted unexpectedly')


def _submission_cooldown_minutes() -> int:
    raw = str(os.getenv('SUBMISSION_COOLDOWN_MINUTES', '120') or '120').strip()
    try:
        return max(0, min(int(raw), 1440))
    except ValueError:
        return 120


def _parse_utc_timestamp(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_recent_submission(last_submitted_at: Any) -> bool:
    submitted_at = _parse_utc_timestamp(last_submitted_at)
    if submitted_at is None:
        return False
    elapsed_minutes = (datetime.now(timezone.utc) - submitted_at).total_seconds() / 60
    return elapsed_minutes <= _submission_cooldown_minutes()


def _is_already_submitted(existing_status: Any, last_submitted_at: Any) -> bool:
    status = str(existing_status or '').strip().lower()
    if status in _ACTIVE_SUBMISSION_STATUSES:
        return True
    # Treat old filled statuses as historical so they do not block fresh routing.
    if status == 'filled':
        return _is_recent_submission(last_submitted_at)
    return False


def _classify_connection_status(detail: str) -> str:
    lowered_detail = str(detail or '').lower()
    if 'unauthorized' in lowered_detail or 'auth failed' in lowered_detail:
        return 'unauthorized'
    if any(marker in lowered_detail for marker in _TRANSIENT_NETWORK_MARKERS):
        return 'network_error'
    return 'error'


@dataclass(slots=True)
class TradingConfig:
    broker: str = 'alpaca'
    trading_mode: str = 'paper'
    auto_submit: bool = False
    approval_mode: str = 'manual'
    use_proxy_equities: bool = True
    account_size_usd: float = 100_000.0
    max_positions: int = 5
    max_position_pct: float = 2.0
    discord_webhook_url: str = ''  # Add this line
    min_option_dte: int = 14
    max_option_dte: int = 45
    target_option_dte_min: int = 21
    target_option_dte_max: int = 45
    api_key: str = ''
    secret_key: str = ''
    base_url: str = PAPER_BASE_URL

    @classmethod
    def from_env(cls) -> 'TradingConfig':
        _load_env_file()
        trading_mode = os.getenv('TRADING_MODE', 'paper').strip().lower()
        broker = os.getenv('BROKER_NAME', 'alpaca').strip().lower()
        auto_submit = _bool_env('AUTO_SUBMIT', False)
        approval_mode = str(os.getenv('APPROVAL_MODE', 'manual') or 'manual').strip().lower()
        if approval_mode not in {'manual', 'automatic'}:
            approval_mode = 'manual'
        use_proxy_equities = _bool_env('USE_PROXY_EQUITIES', True)
        account_size_usd = float(os.getenv('PAPER_ACCOUNT_SIZE', '100000'))
        max_positions = int(os.getenv('MAX_OPEN_POSITIONS', '5'))
        max_position_pct = float(os.getenv('MAX_POSITION_PCT', '2.0'))
        min_option_dte = int(os.getenv('MIN_OPTION_DTE', '14'))
        max_option_dte = int(os.getenv('MAX_OPTION_DAYS', '45'))
        target_option_dte_min = int(os.getenv('TARGET_OPTION_DTE_MIN', '21'))
        target_option_dte_max = int(os.getenv('TARGET_OPTION_DTE_MAX', '45'))

        # Keep DTE bounds consistent even if env values are misconfigured.
        min_option_dte = max(1, min_option_dte)
        max_option_dte = max(min_option_dte, max_option_dte)
        target_option_dte_min = max(min_option_dte, target_option_dte_min)
        target_option_dte_max = max(target_option_dte_min, min(max_option_dte, target_option_dte_max))

        api_key = os.getenv('ALPACA_API_KEY', '').strip()
        secret_key = os.getenv('ALPACA_SECRET_KEY', '').strip()
        default_base_url = PAPER_BASE_URL if trading_mode == 'paper' else LIVE_BASE_URL
        base_url = os.getenv('ALPACA_BASE_URL', default_base_url).strip() or default_base_url
        discord_webhook_url = os.getenv('DISCORD_WEBHOOK_URL', '').strip()

        return cls(
            broker=broker,
            trading_mode=trading_mode,
            auto_submit=auto_submit,
            approval_mode=approval_mode,
            use_proxy_equities=use_proxy_equities,
            account_size_usd=account_size_usd,
            max_positions=max_positions,
            max_position_pct=max_position_pct,
            discord_webhook_url=discord_webhook_url,
            min_option_dte=min_option_dte,
            max_option_dte=max_option_dte,
            target_option_dte_min=target_option_dte_min,
            target_option_dte_max=target_option_dte_max,
            api_key=api_key,
            secret_key=secret_key,
            base_url=base_url,
        )


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _is_assignment_flagged(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'pending', 'flagged'}


def _is_hard_safety_hold(row: pd.Series) -> bool:
    approval_status = str(row.get('approval_status', 'pending') or 'pending').strip().lower()
    if approval_status in {'rejected', 'hold'}:
        return True

    guardrail_status = str(row.get('guardrail_status', '') or '').strip().lower()
    if guardrail_status and guardrail_status not in {'pass', 'passed', 'clear', 'ok', 'approved'}:
        return True

    return False


def _should_auto_approve_paper_row(row: pd.Series, config: TradingConfig) -> bool:
    if config.trading_mode != 'paper':
        return False

    if not _bool_env('AUTO_APPROVE_PAPER_TRADES', False):
        return False

    if not config.auto_submit and config.approval_mode != 'automatic':
        return False

    if _is_hard_safety_hold(row):
        return False

    strict_mode = _bool_env('DISCOVERY_QUALITY_STRICT', True)
    dq_status = str(row.get('discovery_quality_status', '') or '').strip().lower()
    dq_override = _as_bool(row.get('discovery_quality_override', False))
    if dq_status == 'block' and not dq_override and strict_mode:
        return False

    return True


def _resolve_submit_approval(row: pd.Series, config: TradingConfig) -> tuple[str, bool]:
    approval_status = str(row.get('approval_status', 'pending') or 'pending').strip().lower()
    approved_for_submit = approval_status in {'approved', 'auto_approved'} or _as_bool(row.get('approved_for_submit', False))

    strict_mode = _bool_env('DISCOVERY_QUALITY_STRICT', True)
    dq_status = str(row.get('discovery_quality_status', '') or '').strip().lower()
    dq_override = _as_bool(row.get('discovery_quality_override', False))
    dq_blocked = dq_status == 'block' and not dq_override

    if _is_hard_safety_hold(row):
        return 'hold', False

    if dq_blocked and strict_mode:
        return 'hold', False

    if _should_auto_approve_paper_row(row, config):
        approved_for_submit = True
        if approval_status not in {'approved', 'auto_approved'}:
            approval_status = 'auto_approved'
        return approval_status, approved_for_submit

    if config.approval_mode == 'automatic' and approval_status not in {'rejected', 'hold'}:
        approved_for_submit = True
        if approval_status not in {'approved', 'auto_approved'}:
            approval_status = 'auto_approved'

    return approval_status, approved_for_submit


def load_latest_csv(prefix: str) -> pd.DataFrame:
    files = sorted(OUTPUT_DIR.glob(f'{prefix}*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return pd.DataFrame()
    return pd.read_csv(files[0])


def _infer_option_type_and_strike(options_setup: str, bias: str) -> tuple[str, float]:
    text = str(options_setup or '')
    if 'P' in text.upper() or str(bias).lower() == 'bearish':
        option_type = 'put'
    else:
        option_type = 'call'

    strike = 0.0
    match = re.search(r'(\d+(?:\.\d+)?)\s*[CP]', text.upper())
    if match:
        strike = float(match.group(1))
    return option_type, strike


def _looks_like_occ_option_symbol(symbol: str) -> bool:
    text = str(symbol or '').strip().upper()
    # OCC format example: INTC260626C00109000
    return bool(re.fullmatch(r'[A-Z]{1,6}\d{6}[CP]\d{8}', text))


def _intraday_drift(reference: Any, midpoint: Any) -> float | None:
    try:
        reference_price = float(reference)
        live_price = float(midpoint)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(reference_price) or not math.isfinite(live_price) or reference_price <= 0 or live_price <= 0:
        return None
    return (live_price - reference_price) / reference_price


def _option_position_intent(side: str, execution_style: str) -> str:
    normalized_side = str(side or '').strip().lower()
    style = str(execution_style or '').strip().lower()
    if 'protective_exit' in style:
        return 'sell_to_close' if normalized_side == 'sell' else 'buy_to_close'
    return 'buy_to_open' if normalized_side == 'buy' else 'sell_to_open'


def _is_spread_order(order: pd.Series) -> bool:
    spread_type = order.get('spread_type', '')
    has_spread_type = pd.notna(spread_type) and bool(str(spread_type).strip())
    setup = str(order.get('options_setup', '') or '').lower()
    style = str(order.get('execution_style', '') or '').lower()
    return bool(has_spread_type or '/' in setup or 'spread' in setup or 'multi_leg' in style or 'mleg' in style)


def build_paper_debit_vertical_request(
    config: TradingConfig,
    long_symbol: str,
    short_symbol: str,
    quantity: int,
    limit_price: Decimal,
    client_order_id: str,
):
    from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

    if config.broker != 'alpaca' or config.trading_mode != 'paper' or config.base_url.rstrip('/') != PAPER_BASE_URL:
        raise ValueError('Debit vertical requests are restricted to Alpaca paper mode')
    pattern = r'([A-Z]{1,6})(\d{6})([CP])(\d{8})'
    long_match = re.fullmatch(pattern, str(long_symbol))
    short_match = re.fullmatch(pattern, str(short_symbol))
    if not long_match or not short_match or long_match.groups()[:3] != short_match.groups()[:3]:
        raise ValueError('Both legs must have matching underlying, expiration and option type')
    long_strike, short_strike = int(long_match.group(4)), int(short_match.group(4))
    if not (long_strike > 0 and short_strike > 0 and (
        (long_match.group(3) == 'C' and long_strike < short_strike)
        or (long_match.group(3) == 'P' and long_strike > short_strike)
    )):
        raise ValueError('The leg strikes do not form a debit vertical')
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0 or not isinstance(client_order_id, str) or not client_order_id.strip():
        raise ValueError('A positive whole spread quantity and client order ID are required')
    try:
        price = Decimal(str(limit_price))
        if not price.is_finite() or price <= 0 or price != price.quantize(Decimal('0.01')):
            raise ValueError('A positive limit price in cents is required')
        if price >= Decimal(abs(long_strike - short_strike)) / Decimal('1000'):
            raise ValueError('Debit must be less than the vertical strike width')
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('Invalid debit limit price') from exc
    return LimitOrderRequest(
        qty=quantity, limit_price=float(price), time_in_force=TimeInForce.DAY,
        order_class=OrderClass.MLEG, client_order_id=str(client_order_id),
        legs=[
            OptionLegRequest(symbol=long_symbol, ratio_qty=1, side=OrderSide.BUY, position_intent=PositionIntent.BUY_TO_OPEN),
            OptionLegRequest(symbol=short_symbol, ratio_qty=1, side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_OPEN),
        ],
    )


def build_paper_vertical_close_request(
    config: TradingConfig,
    long_symbol: str,
    short_symbol: str,
    quantity: int,
    limit_price: Decimal,
    client_order_id: str,
):
    from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

    if config.broker != 'alpaca' or config.trading_mode != 'paper' or config.base_url.rstrip('/') != PAPER_BASE_URL:
        raise ValueError('Paired close requests are restricted to Alpaca paper mode')
    pattern = r'([A-Z]{1,6})(\d{6})([CP])(\d{8})'
    long_match = re.fullmatch(pattern, str(long_symbol))
    short_match = re.fullmatch(pattern, str(short_symbol))
    if not long_match or not short_match or long_match.groups()[:3] != short_match.groups()[:3]:
        raise ValueError('Both closing legs must have matching underlying, expiration and option type')
    long_strike, short_strike = int(long_match.group(4)), int(short_match.group(4))
    if not (long_strike > 0 and short_strike > 0 and (
        (long_match.group(3) == 'C' and long_strike < short_strike)
        or (long_match.group(3) == 'P' and long_strike > short_strike)
    )):
        raise ValueError('Closing contracts do not form the expected debit vertical')
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0 or not isinstance(client_order_id, str) or not client_order_id.strip():
        raise ValueError('A positive whole spread quantity and client order ID are required')
    try:
        price = Decimal(str(limit_price))
        width = Decimal(abs(long_strike - short_strike)) / Decimal('1000')
        if not price.is_finite() or price == 0 or price != price.quantize(Decimal('0.01')) or abs(price) > width:
            raise ValueError('Paired close limit must be nonzero, in cents, and within vertical width')
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('Invalid paired close limit price') from exc
    return LimitOrderRequest(
        qty=quantity, limit_price=float(price), time_in_force=TimeInForce.DAY,
        order_class=OrderClass.MLEG, client_order_id=client_order_id,
        legs=[
            OptionLegRequest(symbol=long_symbol, ratio_qty=1, side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_CLOSE),
            OptionLegRequest(symbol=short_symbol, ratio_qty=1, side=OrderSide.BUY, position_intent=PositionIntent.BUY_TO_CLOSE),
        ],
    )


def validate_paper_spread_account(config: TradingConfig, order: pd.Series) -> tuple[str, str]:
    if config.broker != 'alpaca' or config.trading_mode != 'paper' or config.base_url.rstrip('/') != PAPER_BASE_URL:
        return 'REJECTED_PAPER_ACCOUNT_REQUIRED', 'Spread orders require the Alpaca paper account endpoint.'
    if not config.api_key or not config.secret_key:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Paper account credentials are unavailable.'
    try:
        account = _fetch_alpaca_json(config, '/v2/account')
        if str(account.get('status', '')).strip().lower() != 'active' or any(
            _as_bool(account.get(name, False))
            for name in ('account_blocked', 'trading_blocked', 'trade_suspended_by_user')
        ):
            return 'REJECTED_ACCOUNT_STATUS', 'Paper account is inactive or trading is blocked.'
        approved_level = int(account.get('options_approved_level', 0) or 0)
        trading_level = int(account.get('options_trading_level', 0) or 0)
        if min(approved_level, trading_level) < 3:
            return 'REJECTED_OPTIONS_PERMISSION', 'Alpaca options level 3 approval is required for multi-leg spreads.'
        buying_power = float(account.get('options_buying_power'))
        limit_price = Decimal(str(order.get('limit_debit', '')))
        quantity = int(order.get('proxy_qty', 0))
        required = limit_price * quantity * 100
        if not math.isfinite(buying_power) or buying_power <= 0 or not limit_price.is_finite() or limit_price <= 0 or quantity <= 0:
            return 'REJECTED_ACCOUNT_STATE', 'Fresh options buying power or spread risk data is invalid.'
        if required > Decimal(str(buying_power)):
            return 'REJECTED_BUYING_POWER', 'Spread debit exceeds current options buying power.'
        risk_budget = Decimal(str(order.get('max_risk_usd', order.get('spread_risk_budget_usd', 0))))
        if not risk_budget.is_finite() or required > risk_budget:
            return 'REJECTED_RISK_BUDGET', 'Spread debit exceeds the configured risk budget.'
        return '', ''
    except Exception as exc:
        return 'REJECTED_ACCOUNT_STATE', f'Fresh paper account state is unavailable ({type(exc).__name__}).'


def _lookup_order_by_client_order_id(config: TradingConfig, client_order_id: str) -> dict[str, Any] | None:
    from urllib.parse import quote

    endpoint = f'/v2/orders:by_client_order_id?client_order_id={quote(client_order_id, safe="")}'
    try:
        body = _fetch_alpaca_json(config, endpoint)
    except requests.HTTPError as exc:
        response = getattr(exc, 'response', None) or getattr(getattr(exc, '__cause__', None), 'response', None)
        if getattr(response, 'status_code', None) == 404:
            return None
        raise
    if not isinstance(body, dict):
        raise ValueError('Broker order reconciliation returned a malformed response')
    return body


def _debit_vertical_submission_checks(
    order: pd.Series,
    config: TradingConfig,
) -> tuple[str, str, str, Any | None, Decimal | None]:
    if config.broker != 'alpaca' or config.trading_mode != 'paper' or config.base_url.rstrip('/') != PAPER_BASE_URL:
        return 'REJECTED_PAPER_ACCOUNT_REQUIRED', 'Debit vertical submissions are restricted to the Alpaca paper endpoint.', '', None, None
    if not config.api_key or not config.secret_key:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Paper market-data credentials are unavailable.', '', None, None

    ticker = str(order.get('underlying_ticker', order.get('ticker', ''))).strip().upper()
    long_symbol = str(order.get('long_leg_symbol', '')).strip().upper()
    short_symbol = str(order.get('short_leg_symbol', '')).strip().upper()
    quantity = pd.to_numeric(order.get('proxy_qty'), errors='coerce')
    reference = pd.to_numeric(order.get('signal_close'), errors='coerce')
    risk_budget = pd.to_numeric(order.get('spread_risk_budget_usd', order.get('max_risk_usd')), errors='coerce')
    if (
        not ticker or not _looks_like_occ_option_symbol(long_symbol) or not _looks_like_occ_option_symbol(short_symbol)
        or pd.isna(quantity) or float(quantity) <= 0 or int(quantity) != float(quantity)
        or pd.isna(reference) or float(reference) <= 0
        or pd.isna(risk_budget) or float(risk_budget) <= 0
    ):
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Spread legs, quantity, reference close, or risk budget are invalid.', '', None, None

    try:
        from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient

        from .market_data import option_snapshot_metrics, stock_quote_midpoint

        max_age = float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60'))
        max_spread = float(os.getenv('MAX_OPTION_SPREAD_PCT', '0.06'))
        if not math.isfinite(max_age) or max_age <= 0 or not math.isfinite(max_spread) or max_spread < 0:
            raise ValueError('Invalid quote age or spread threshold')
        stock_client = StockHistoricalDataClient(config.api_key, config.secret_key)
        option_client = OptionHistoricalDataClient(config.api_key, config.secret_key)
        underlying_midpoint = stock_quote_midpoint(stock_client, ticker, max_age)
        long_midpoint, long_spread, _ = option_snapshot_metrics(option_client, long_symbol, max_age)
        short_midpoint, short_spread, _ = option_snapshot_metrics(option_client, short_symbol, max_age)
        if any(not math.isfinite(value) or value < 0 for value in (long_spread, short_spread)):
            raise ValueError('Invalid leg spread')
        if long_spread > max_spread or short_spread > max_spread:
            return 'REJECTED_SPREAD_WIDTH', 'A fresh option leg quote exceeds the configured spread-width limit.', '', None, None

        long_ask = Decimal(str(long_midpoint)) * (Decimal('1') + Decimal(str(long_spread)) / 2)
        short_bid = Decimal(str(short_midpoint)) * (Decimal('1') - Decimal(str(short_spread)) / 2)
        debit = long_ask - short_bid
        limit_debit = debit.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        if not debit.is_finite() or debit <= 0 or limit_debit <= 0:
            return 'REJECTED_SPREAD_DEBIT', 'Fresh leg quotes do not produce a positive debit.', '', None, None

        guard_row = order.copy()
        guard_row['ticker'] = ticker
        guard_code, guard_detail, rank_source = _validate_debit_vertical_guardrails(
            guard_row, config, underlying_midpoint, max(long_spread, short_spread), option_client,
        )
        if guard_code:
            return guard_code, guard_detail, rank_source, None, None

        guarded_order = order.copy()
        guarded_order['limit_debit'] = float(limit_debit)
        guarded_order['proxy_qty'] = int(quantity)
        guarded_order['spread_risk_budget_usd'] = float(risk_budget)
        account_code, account_detail = validate_paper_spread_account(config, guarded_order)
        if account_code:
            return account_code, account_detail, rank_source, None, None
        if limit_debit * int(quantity) * 100 > Decimal(str(float(risk_budget))):
            return 'REJECTED_RISK_BUDGET', 'Fresh spread debit exceeds the configured risk budget.', rank_source, None, None

        request = build_paper_debit_vertical_request(
            config, long_symbol, short_symbol, int(quantity), limit_debit, str(order.get('client_order_id', '')).strip(),
        )
        return '', '', rank_source, request, limit_debit
    except (ValueError, TypeError, AttributeError, KeyError, InvalidOperation) as exc:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', f'Fresh spread validation failed ({type(exc).__name__}).', '', None, None
    except Exception as exc:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', f'Fresh spread market or safety data is unavailable ({type(exc).__name__}).', '', None, None


def _fresh_vertical_close_request(
    order: pd.Series,
    config: TradingConfig,
    option_quote_client=None,
) -> tuple[str, str, Any | None, Decimal | None]:
    if config.broker != 'alpaca' or config.trading_mode != 'paper' or config.base_url.rstrip('/') != PAPER_BASE_URL:
        return 'REJECTED_PAPER_ACCOUNT_REQUIRED', 'Paired closes are restricted to the Alpaca paper endpoint.', None, None
    if not config.api_key or not config.secret_key:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Paper market-data credentials are unavailable.', None, None

    long_symbol = str(order.get('long_leg_symbol', '')).strip().upper()
    short_symbol = str(order.get('short_leg_symbol', '')).strip().upper()
    quantity = pd.to_numeric(order.get('proxy_qty'), errors='coerce')
    if (
        not _looks_like_occ_option_symbol(long_symbol) or not _looks_like_occ_option_symbol(short_symbol)
        or pd.isna(quantity) or float(quantity) <= 0 or int(quantity) != float(quantity)
    ):
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Both option legs and a positive whole close quantity are required.', None, None

    try:
        from alpaca.data.historical import OptionHistoricalDataClient

        from .market_data import option_snapshot_metrics

        max_age = float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60'))
        max_spread = float(os.getenv('MAX_OPTION_SPREAD_PCT', '0.06'))
        if not math.isfinite(max_age) or max_age <= 0 or not math.isfinite(max_spread) or max_spread < 0:
            raise ValueError('Invalid quote age or spread threshold')
        option_client = option_quote_client or OptionHistoricalDataClient(config.api_key, config.secret_key)
        long_midpoint, long_spread, _ = option_snapshot_metrics(option_client, long_symbol, max_age)
        short_midpoint, short_spread, _ = option_snapshot_metrics(option_client, short_symbol, max_age)
        if any(not math.isfinite(value) or value < 0 for value in (long_spread, short_spread)):
            raise ValueError('Invalid leg spread')
        if long_spread > max_spread or short_spread > max_spread:
            return 'REJECTED_SPREAD_WIDTH', 'A fresh option leg quote exceeds the configured spread-width limit.', None, None

        long_bid = Decimal(str(long_midpoint)) * (Decimal('1') - Decimal(str(long_spread)) / 2)
        short_ask = Decimal(str(short_midpoint)) * (Decimal('1') + Decimal(str(short_spread)) / 2)
        signed_limit = (short_ask - long_bid).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        if signed_limit == 0:
            return 'REJECTED_SPREAD_DEBIT', 'Fresh leg quotes do not produce a nonzero paired-close limit.', None, None
        request = build_paper_vertical_close_request(
            config, long_symbol, short_symbol, int(quantity), signed_limit,
            str(order.get('client_order_id', '')).strip(),
        )
        return '', '', request, signed_limit
    except (ValueError, TypeError, AttributeError, KeyError, InvalidOperation) as exc:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', f'Fresh paired-close validation failed ({type(exc).__name__}).', None, None
    except Exception as exc:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', f'Fresh paired-close quotes are unavailable ({type(exc).__name__}).', None, None


def _spread_fill_reconciliation(order: dict[str, Any]) -> str:
    legs = order.get('legs')
    parent_filled = pd.to_numeric(order.get('filled_qty'), errors='coerce')
    if not isinstance(legs, list) or len(legs) != 2 or pd.isna(parent_filled):
        return 'unknown'
    leg_fills = [pd.to_numeric(leg.get('filled_qty'), errors='coerce') for leg in legs if isinstance(leg, dict)]
    if len(leg_fills) != 2 or any(pd.isna(value) for value in leg_fills):
        return 'unknown'
    if abs(float(leg_fills[0]) - float(leg_fills[1])) > 1e-9 or abs(float(leg_fills[0]) - float(parent_filled)) > 1e-9:
        return 'mismatched'
    return 'matched'


def _fresh_close_spread_state(order: pd.Series, config: TradingConfig) -> tuple[str, str]:
    long_symbol = str(order.get('long_leg_symbol', '')).strip().upper()
    short_symbol = str(order.get('short_leg_symbol', '')).strip().upper()
    expected_symbols = {long_symbol, short_symbol}
    try:
        positions = _fetch_alpaca_json(config, '/v2/positions')
        broker_orders = _fetch_alpaca_json(config, '/v2/orders?status=all&limit=500&direction=desc&nested=true')
    except Exception as exc:
        return 'MANUAL_REVIEW_REQUIRED', f'Fresh broker spread state is unavailable ({type(exc).__name__}).'
    if not isinstance(positions, list) or not isinstance(broker_orders, list):
        return 'MANUAL_REVIEW_REQUIRED', 'Fresh broker spread state is malformed.'

    spread_positions = [
        position for position in positions
        if isinstance(position, dict) and str(position.get('symbol', '')).strip().upper() in expected_symbols
    ]
    if len(spread_positions) != 2 or {
        str(position.get('symbol', '')).strip().upper() for position in spread_positions
    } != expected_symbols:
        return 'MANUAL_REVIEW_REQUIRED', 'A spread leg is missing from the current broker positions.'
    sides = {
        str(position.get('symbol', '')).strip().upper(): str(position.get('side', '')).strip().lower()
        for position in spread_positions
    }
    quantities = [
        pd.to_numeric(position.get('qty'), errors='coerce') for position in spread_positions
    ]
    requested_qty = pd.to_numeric(order.get('proxy_qty'), errors='coerce')
    if (
        set(sides.values()) != {'long', 'short'} or any(pd.isna(qty) or float(qty) <= 0 for qty in quantities)
        or abs(float(quantities[0]) - float(quantities[1])) > 1e-9
        or pd.isna(requested_qty) or float(requested_qty) > min(float(qty) for qty in quantities)
    ):
        return 'MANUAL_REVIEW_REQUIRED', 'Fresh broker spread legs or quantities are mismatched.'
    assignment_names = (
        'early_assignment', 'early_assignment_flag', 'early_assignment_risk',
        'assignment_pending', 'assignment_flag', 'exercise_pending',
    )
    if any(_is_assignment_flagged(position.get(name, False)) for position in spread_positions for name in assignment_names):
        return 'MANUAL_REVIEW_REQUIRED', 'A fresh broker assignment or exercise flag requires manual review.'

    for broker_order in broker_orders:
        if not isinstance(broker_order, dict) or str(broker_order.get('order_class', '')).strip().lower() != 'mleg':
            continue
        legs = broker_order.get('legs') if isinstance(broker_order.get('legs'), list) else []
        order_symbols = {
            str(leg.get('symbol', '')).strip().upper()
            for leg in legs if isinstance(leg, dict) and leg.get('symbol')
        }
        underlying = str(broker_order.get('underlying_symbol', '')).strip().upper()
        if order_symbols and order_symbols != expected_symbols:
            continue
        if not order_symbols and underlying != str(order.get('underlying_ticker', '')).strip().upper():
            continue
        if any(_is_assignment_flagged(leg.get(name, False)) for leg in legs if isinstance(leg, dict) for name in assignment_names):
            return 'MANUAL_REVIEW_REQUIRED', 'A broker-reported assignment or exercise flag requires manual review.'
        status = str(broker_order.get('status', '')).strip().lower()
        if status == 'partially_filled' or status in {'new', 'accepted', 'pending_new', 'submitted'}:
            return 'MANUAL_REVIEW_REQUIRED', 'A broker spread fill is partial or still pending.'
        if status == 'filled' and (
            order_symbols != expected_symbols or _spread_fill_reconciliation(broker_order) != 'matched'
        ):
            return 'MANUAL_REVIEW_REQUIRED', 'Broker spread leg fills are not reconciled.'
    return '', ''


def _mleg_submission_result(
    order: pd.Series,
    config: TradingConfig,
    body: dict[str, Any] | None,
    request_error: Exception | None = None,
    reconcile: bool = True,
) -> dict[str, Any]:
    client_order_id = str(order.get('client_order_id', '')).strip()
    reconciliation_error = ''
    if reconcile:
        try:
            reconciled = _lookup_order_by_client_order_id(config, client_order_id)
        except Exception as exc:
            reconciled = None
            reconciliation_error = f'Order lookup failed ({type(exc).__name__}).'
        else:
            if reconciled is not None:
                body = reconciled
    else:
        reconciled = None

    if request_error is not None and reconciled is None:
        return {
            'status': 'unknown',
            'rejection_code': 'REJECTED_ORDER_RECONCILIATION',
            'detail': f'MLeg submission outcome is ambiguous; no retry was sent. {reconciliation_error or "Client order ID was not found during reconciliation."}',
            'spread_reconciliation_status': 'unknown',
        }
    if body is None:
        return {
            'status': 'unknown',
            'rejection_code': 'REJECTED_ORDER_RECONCILIATION',
            'detail': f'MLeg response was incomplete and could not be reconciled. {reconciliation_error or "No order was found by client order ID."}',
            'spread_reconciliation_status': 'unknown',
        }

    status = str(body.get('status', 'unknown')).strip().lower()
    reconciliation = _spread_fill_reconciliation(body)
    if status in {'partially_filled', 'pending_new', 'new', 'accepted', 'submitted'} and reconciliation == 'unknown':
        reconciliation = 'unknown'
    result: dict[str, Any] = {
        'status': status,
        'detail': str(body.get('id', client_order_id)),
        'broker_order_id': str(body.get('id', '')),
        'filled_qty': body.get('filled_qty', 0),
        'spread_reconciliation_status': reconciliation,
    }
    if status == 'unknown':
        result['rejection_code'] = 'REJECTED_ORDER_RECONCILIATION'
        result['detail'] = 'Broker response did not contain a usable order status.'
    return result


def _submit_paper_debit_vertical(
    order: pd.Series,
    config: TradingConfig,
    base_result: dict[str, Any],
) -> dict[str, Any]:
    if config.trading_mode != 'paper' or config.broker != 'alpaca' or config.base_url.rstrip('/') != PAPER_BASE_URL:
        return {**base_result, 'status': 'blocked', 'rejection_code': 'REJECTED_PAPER_ACCOUNT_REQUIRED',
                'detail': 'Debit vertical submissions are restricted to the Alpaca paper endpoint.'}
    if not _as_bool(order.get('approved_for_submit', False)) or not _as_bool(order.get('ready_for_broker_submit', False)):
        return {**base_result, 'status': 'blocked', 'detail': 'Spread preview is not approved and ready for broker submission.'}
    if _as_bool(order.get('already_submitted', False)):
        return {**base_result, 'status': 'skipped_existing', 'detail': 'Spread entry was already submitted.'}
    if not config.auto_submit:
        return {**base_result, 'status': 'preview_only', 'detail': 'AUTO_SUBMIT is disabled. No broker order was sent.'}
    if not config.api_key or not config.secret_key:
        return {**base_result, 'status': 'missing_credentials', 'detail': 'Alpaca paper credentials are not configured.'}

    client_order_id = str(order.get('client_order_id', '')).strip()
    if not client_order_id:
        return {**base_result, 'status': 'blocked', 'rejection_code': 'REJECTED_ORDER_RECONCILIATION',
                'detail': 'A stable client order ID is required for MLeg submission.'}
    try:
        existing = _lookup_order_by_client_order_id(config, client_order_id)
    except Exception as exc:
        return {**base_result, 'status': 'unknown', 'rejection_code': 'REJECTED_ORDER_RECONCILIATION',
                'detail': f'Could not verify the MLeg client order ID before submission ({type(exc).__name__}); no order was sent.'}
    if existing is not None:
        reconciled = _mleg_submission_result(order, config, existing, reconcile=False)
        return {**base_result, **reconciled, 'detail': f'Existing client order ID reconciled: {reconciled["detail"]}'}

    code, detail, rank_source, request, limit_debit = _debit_vertical_submission_checks(order, config)
    if code:
        return {**base_result, 'status': 'blocked', 'rejection_code': code, 'detail': detail,
                'iv_rank_source': rank_source}
    if request is None or limit_debit is None:
        return {**base_result, 'status': 'blocked', 'rejection_code': 'REJECTED_SAFETY_DATA_UNAVAILABLE',
                'detail': 'Fresh spread validation did not produce a broker request.', 'iv_rank_source': rank_source}

    payload = request.model_dump(exclude_none=True, mode='json')
    base_result = {**base_result, 'limit_price': format(limit_debit, '.2f'), 'iv_rank_source': rank_source}
    try:
        response = requests.request(
            method='POST', url=f'{config.base_url}/v2/orders', headers=_broker_headers(config),
            json=payload, timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not body.get('id') or not body.get('status'):
            reconciled = _mleg_submission_result(order, config, body if isinstance(body, dict) else None)
            return {**base_result, **reconciled}
        status = str(body.get('status', '')).strip().lower()
        if status == 'partially_filled':
            reconciled = _mleg_submission_result(order, config, body)
            return {**base_result, **reconciled}
        return {**base_result, **_mleg_submission_result(order, config, body, reconcile=False)}
    except Exception as exc:
        reconciled = _mleg_submission_result(order, config, None, request_error=exc)
        return {**base_result, **reconciled}


def _submit_paper_vertical_close(
    order: pd.Series,
    config: TradingConfig,
    base_result: dict[str, Any],
) -> dict[str, Any]:
    base_result = {
        **base_result,
        'paired_symbols': str(order.get('paired_symbols', '')),
        'spread_group_key': str(order.get('spread_group_key', '')),
    }
    if config.trading_mode != 'paper' or config.broker != 'alpaca' or config.base_url.rstrip('/') != PAPER_BASE_URL:
        return {**base_result, 'status': 'blocked', 'rejection_code': 'REJECTED_PAPER_ACCOUNT_REQUIRED',
                'detail': 'Paired closes are restricted to the Alpaca paper endpoint.'}
    if str(order.get('spread_lifecycle_status', '')).strip().lower() != 'healthy_matched':
        return {**base_result, 'status': 'blocked', 'rejection_code': 'MANUAL_REVIEW_REQUIRED',
                'detail': 'Only a healthy, matched spread can be closed automatically.'}
    if not _bool_env('ENABLE_EXIT_AUTOMATION', False):
        return {**base_result, 'status': 'blocked', 'detail': 'ENABLE_EXIT_AUTOMATION is disabled.'}
    if not _as_bool(order.get('approved_for_submit', False)) or not _as_bool(order.get('ready_for_broker_submit', False)):
        return {**base_result, 'status': 'blocked', 'detail': 'Paired-close preview is not approved and ready for broker submission.'}
    if _as_bool(order.get('already_submitted', False)):
        return {**base_result, 'status': 'skipped_existing', 'detail': 'Paired close was already submitted.'}
    if not config.auto_submit:
        return {**base_result, 'status': 'preview_only', 'detail': 'AUTO_SUBMIT is disabled. No broker order was sent.'}
    if not config.api_key or not config.secret_key:
        return {**base_result, 'status': 'missing_credentials', 'detail': 'Alpaca paper credentials are not configured.'}
    if any(_is_assignment_flagged(order.get(name, False)) for name in (
        'early_assignment', 'early_assignment_flag', 'assignment_pending', 'exercise_pending',
    )):
        return {**base_result, 'status': 'blocked', 'rejection_code': 'MANUAL_REVIEW_REQUIRED',
                'detail': 'An assignment or exercise flag halted the paired close.'}

    client_order_id = str(order.get('client_order_id', '')).strip()
    if not client_order_id:
        return {**base_result, 'status': 'blocked', 'rejection_code': 'REJECTED_ORDER_RECONCILIATION',
                'detail': 'A stable client order ID is required for paired-close submission.'}
    try:
        existing = _lookup_order_by_client_order_id(config, client_order_id)
    except Exception as exc:
        return {**base_result, 'status': 'unknown', 'rejection_code': 'REJECTED_ORDER_RECONCILIATION',
                'detail': f'Could not verify the paired-close client order ID before submission ({type(exc).__name__}); no order was sent.'}
    if existing is not None:
        reconciled = _mleg_submission_result(order, config, existing, reconcile=False)
        return {**base_result, **reconciled, 'detail': f'Existing client order ID reconciled: {reconciled["detail"]}'}

    state_code, state_detail = _fresh_close_spread_state(order, config)
    if state_code:
        return {**base_result, 'status': 'blocked', 'rejection_code': state_code, 'detail': state_detail}

    code, detail, request, limit_price = _fresh_vertical_close_request(order, config)
    if code:
        return {**base_result, 'status': 'blocked', 'rejection_code': code, 'detail': detail}
    if request is None or limit_price is None:
        return {**base_result, 'status': 'blocked', 'rejection_code': 'REJECTED_SAFETY_DATA_UNAVAILABLE',
                'detail': 'Fresh paired-close validation did not produce a broker request.'}

    payload = request.model_dump(exclude_none=True, mode='json')
    base_result = {**base_result, 'limit_price': format(limit_price, '.2f')}
    try:
        response = requests.request(
            method='POST', url=f'{config.base_url}/v2/orders', headers=_broker_headers(config),
            json=payload, timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not body.get('id') or not body.get('status'):
            reconciled = _mleg_submission_result(order, config, body if isinstance(body, dict) else None)
            return {**base_result, **reconciled}
        status = str(body.get('status', '')).strip().lower()
        if status == 'partially_filled':
            reconciled = _mleg_submission_result(order, config, body)
            return {**base_result, **reconciled}
        return {**base_result, **_mleg_submission_result(order, config, body, reconcile=False)}
    except Exception as exc:
        reconciled = _mleg_submission_result(order, config, None, request_error=exc)
        return {**base_result, **reconciled}


def fetch_option_contracts(
    config: TradingConfig,
    underlying_symbol: str,
    min_days: int | None = None,
    max_days: int | None = None,
    limit: int = 200,
    target_strike: float | None = None,
    strike_window_pct: float = 0.25,
) -> pd.DataFrame:
    """Fetch active option contracts for an underlying within the given DTE window.

    When `target_strike` is supplied the API request is narrowed to a
    ±strike_window_pct band around that strike, preventing the 200-contract
    limit from cutting off at-the-money contracts for high-priced underlyings.
    """
    if config.broker != 'alpaca' or not config.api_key or not config.secret_key:
        return pd.DataFrame()

    min_days = int(min_days if min_days is not None else config.min_option_dte)
    max_days = int(max_days if max_days is not None else config.max_option_dte)
    min_days = max(1, min_days)
    max_days = max(min_days, max_days)

    today = pd.Timestamp.now().normalize()
    expiry_gte = (today + pd.Timedelta(days=min_days)).strftime('%Y-%m-%d')
    expiry_lte = (today + pd.Timedelta(days=max_days)).strftime('%Y-%m-%d')

    params: dict = {
        'underlying_symbols': underlying_symbol,
        'status': 'active',
        'expiration_date_gte': expiry_gte,
        'expiration_date_lte': expiry_lte,
        'limit': limit,
    }
    if target_strike and target_strike > 0:
        window = max(5.0, target_strike * strike_window_pct)
        params['strike_price_gte'] = round(target_strike - window, 2)
        params['strike_price_lte'] = round(target_strike + window, 2)

    all_contracts: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    try:
        while True:
            response = requests.get(
                f"{config.base_url}/v2/options/contracts",
                headers=_broker_headers(config),
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            page = body.get('option_contracts')
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ValueError('Malformed option contract page')
            all_contracts.extend(page)
            token = body.get('next_page_token')
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                raise ValueError('Invalid or repeated option contract page token')
            seen_tokens.add(token)
            params['page_token'] = token
    except requests.HTTPError as exc:
        detail = _describe_broker_auth_failure(config, '/v2/options/contracts', exc)
        print(f'Option contract lookup skipped for {underlying_symbol}: {detail}')
        return pd.DataFrame()
    except requests.RequestException as exc:
        print(f'Option contract lookup failed for {underlying_symbol}: {exc}')
        return pd.DataFrame()
    except (ValueError, AttributeError) as exc:
        print(f'Option contract lookup failed for {underlying_symbol}: {exc}')
        return pd.DataFrame()

    contracts = pd.DataFrame(all_contracts)
    if contracts.empty:
        return contracts
    if 'expiration_date' not in contracts.columns:
        return pd.DataFrame()

    contracts['expiration_date'] = pd.to_datetime(contracts['expiration_date'], errors='coerce')
    today = pd.Timestamp.now().normalize()
    contracts['days_to_expiration'] = (contracts['expiration_date'] - today).dt.days
    contracts = contracts[(contracts['days_to_expiration'] >= min_days) & (contracts['days_to_expiration'] <= max_days)].copy()
    return contracts


def choose_best_option_contract(
    contracts: pd.DataFrame,
    underlying_symbol: str,
    option_type: str,
    target_strike: float,
    min_days: int = 14,
    max_days: int = 45,
    target_min_days: int = 21,
    target_max_days: int = 45,
    target_days: int | None = None,
) -> dict[str, Any] | None:
    if contracts.empty:
        return None

    working = contracts.copy()
    working = working[working['underlying_symbol'].astype(str).str.upper() == str(underlying_symbol).upper()]
    working = working[working['type'].astype(str).str.lower() == str(option_type).lower()]
    if 'tradable' in working.columns:
        working = working[working['tradable'].fillna(False).astype(bool)]

    if 'expiration_date' in working.columns:
        working['expiration_date'] = pd.to_datetime(working['expiration_date'], errors='coerce')
        today = pd.Timestamp.now().normalize()
        working['days_to_expiration'] = (working['expiration_date'] - today).dt.days
    elif 'days_to_expiration' not in working.columns:
        working['days_to_expiration'] = max_days + 1

    min_days = max(1, int(min_days))
    max_days = max(min_days, int(max_days))
    target_min_days = max(min_days, int(target_min_days))
    target_max_days = max(target_min_days, min(max_days, int(target_max_days)))
    target_days = int(target_days) if target_days is not None else (target_min_days + target_max_days) // 2
    target_days = max(min_days, min(max_days, target_days))

    working = working[(working['days_to_expiration'] >= min_days) & (working['days_to_expiration'] <= max_days)]
    if working.empty:
        return None

    working['strike_price'] = pd.to_numeric(working.get('strike_price'), errors='coerce').fillna(0.0)
    if 'open_interest' not in working.columns:
        working['open_interest'] = 0
    working['open_interest'] = pd.to_numeric(working['open_interest'], errors='coerce').fillna(0)
    working['strike_distance'] = (working['strike_price'] - float(target_strike or 0.0)).abs()

    dte = pd.to_numeric(working['days_to_expiration'], errors='coerce').fillna(max_days).astype(int)
    in_window = (dte >= target_min_days) & (dte <= target_max_days)
    dte_distance = dte.apply(
        lambda v: 0 if target_min_days <= v <= target_max_days else min(abs(v - target_min_days), abs(v - target_max_days))
    )
    target_center_distance = (dte - target_days).abs()
    working['in_target_window'] = in_window.astype(int)
    working['target_window_distance'] = dte_distance
    working['target_center_distance'] = target_center_distance

    working = working.sort_values(
        ['in_target_window', 'target_window_distance', 'target_center_distance', 'strike_distance', 'open_interest', 'days_to_expiration'],
        ascending=[False, True, True, True, False, True],
    )
    return working.iloc[0].to_dict()


def _strike_mismatch_too_large(target_strike: float, chosen_strike: float) -> bool:
    target = float(target_strike or 0.0)
    chosen = float(chosen_strike or 0.0)
    if target <= 0 or chosen <= 0:
        return False

    allowed_distance = max(5.0, target * 0.10)
    return abs(chosen - target) > allowed_distance


def _resolve_target_days_from_row(row: pd.Series, default_days: int) -> int:
    expiration_target = str(row.get('expiration_target', '') or '').strip()
    if expiration_target and expiration_target.lower() != 'unavailable':
        target_dt = pd.to_datetime(expiration_target, errors='coerce')
        if pd.notna(target_dt):
            target_days = int((target_dt.normalize() - pd.Timestamp.now().normalize()).days)
            if target_days > 0:
                return target_days

    numeric_dte = pd.to_numeric(row.get('days_to_expiration', None), errors='coerce')
    if pd.notna(numeric_dte):
        parsed = int(numeric_dte)
        if parsed > 0:
            return parsed

    return max(1, int(default_days))


def _validate_debit_vertical_guardrails(
    row: pd.Series,
    config: TradingConfig,
    midpoint: float,
    option_spread: float,
    option_client: Any,
) -> tuple[str, str, str]:
    from zoneinfo import ZoneInfo

    from alpaca.data.historical import NewsClient

    from .catalyst_events import count_recent_headlines, get_earnings_dates
    from .iv_history import calculate_iv_rank, safety_database_path
    from .market_data import option_snapshot_metrics
    from .option_guardrails import evaluate_entry

    ticker = str(row.get('ticker', '')).strip().upper()
    try:
        reference = float(row.get('signal_close', 0))
        max_age = float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60'))
        max_drift = float(os.getenv('MAX_INTRADAY_DRIFT_PCT', '0.05'))
        max_spread = float(os.getenv('MAX_OPTION_SPREAD_PCT', '0.06'))
        max_iv_rank = float(os.getenv('MAX_SINGLE_LEG_IV_RANK', '80.0'))
        blackout_days = int(os.getenv('EARNINGS_BLACKOUT_DAYS', '7'))
        news_threshold = int(os.getenv('BREAKING_NEWS_COUNT_THRESHOLD', '3'))
        news_move = float(os.getenv('BREAKING_NEWS_MOVE_PCT', '0.04'))
        if config.trading_mode != 'paper' or not ticker or not config.api_key or not config.secret_key:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Paper market-data credentials or ticker are unavailable.', ''
        if not math.isfinite(reference) or reference <= 0 or option_client is None:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'A valid reference close and option-data client are required.', ''
        now = datetime.now(timezone.utc)
        today = now.astimezone(ZoneInfo('America/New_York')).date()
        database = safety_database_path(PROJECT_DIR)
        earnings = get_earnings_dates(database, ticker, os.getenv('FINNHUB_API_KEY', ''), today, blackout_days)
        headlines = count_recent_headlines(NewsClient(config.api_key, config.secret_key), ticker, now, news_threshold)
        atm_contracts = fetch_option_contracts(config, ticker, min_days=25, max_days=35, target_strike=midpoint)
        atm_contract = choose_best_option_contract(
            atm_contracts, ticker, 'call', midpoint, min_days=25, max_days=35,
            target_min_days=25, target_max_days=35, target_days=30,
        )
        atm_strike = float(atm_contract.get('strike_price', 0)) if atm_contract else 0.0
        if not math.isfinite(atm_strike) or atm_strike <= 0 or abs(atm_strike - midpoint) / midpoint > 0.05:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'A comparable 30-DTE ATM call is unavailable.', 'iv_history'
        _, _, atm_iv = option_snapshot_metrics(option_client, str(atm_contract['symbol']), max_age)
        rank, observations = calculate_iv_rank(database, ticker, atm_iv, today)
        rank_source = 'iv_history'
        if rank is None:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', f'IV rank unavailable ({observations} historical observations).', rank_source
        decision = evaluate_entry(
            reference, midpoint, option_spread, earnings, headlines, rank, today,
            max_drift, max_spread, blackout_days, news_threshold, news_move, max_iv_rank,
        )
        return decision.code, decision.detail, rank_source
    except Exception as exc:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', f'Spread safety data unavailable ({type(exc).__name__}).', ''


def _preview_debit_vertical(
    row: pd.Series,
    config: TradingConfig,
    contracts: pd.DataFrame,
    option_client: Any,
    underlying_midpoint: float,
) -> dict[str, Any]:
    from .market_data import option_snapshot_metrics

    underlying = str(row.get('ticker', '')).strip().upper()
    identity = '|'.join(str(row.get(key, '')) for key in (
        'ticker', 'spread_type', 'expiration_target', 'spread_long_strike', 'spread_short_strike', 'signal_close',
    ))
    order_digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    result: dict[str, Any] = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'ticker': underlying, 'underlying_ticker': underlying,
        'proxy_side': 'buy', 'proxy_qty': 0,
        'execution_mode': config.trading_mode, 'execution_style': 'multi_leg_option_paper',
        'client_order_id': f'{config.trading_mode}-spread-{underlying}-{order_digest}',
        'approved_for_submit': False, 'ready_for_broker_submit': False,
        'rejection_code': 'REJECTED_SPREAD_CONTRACTS',
        'rejection_detail': 'Spread metadata or matching option contracts are unavailable.',
    }
    try:
        spread_type = str(row.get('spread_type', '')).strip()
        option_type = str(row.get('spread_option_type', '')).strip().lower()
        bias = str(row.get('bias', '')).strip().lower()
        long_strike = float(row.get('spread_long_strike'))
        short_strike = float(row.get('spread_short_strike'))
        ratio = float(row.get('spread_ratio_qty'))
        expiry_text = str(row.get('expiration_target', '')).strip()
        expiry = pd.to_datetime(expiry_text, format='%Y-%m-%d', errors='coerce')
        if pd.isna(expiry) or expiry.strftime('%Y-%m-%d') != expiry_text:
            return result
        days = (expiry.normalize() - pd.Timestamp.now().normalize()).days
        if not (21 <= days <= 45) or not all(math.isfinite(value) and value > 0 for value in (long_strike, short_strike)) or ratio != 1:
            return result
        if not ((spread_type == 'bull_call_debit' and option_type == 'call' and bias == 'bullish' and long_strike < short_strike)
                or (spread_type == 'bear_put_debit' and option_type == 'put' and bias == 'bearish' and long_strike > short_strike)):
            return result
        required = {'symbol', 'underlying_symbol', 'type', 'tradable', 'expiration_date', 'strike_price', 'open_interest'}
        if not required.issubset(contracts.columns):
            return result
        eligible = contracts.copy()
        eligible = eligible[
            eligible['underlying_symbol'].astype(str).str.upper().eq(underlying)
            & eligible['type'].astype(str).str.lower().eq(option_type)
            & eligible['tradable'].eq(True)
            & pd.to_datetime(eligible['expiration_date'], errors='coerce').dt.strftime('%Y-%m-%d').eq(expiry_text)
            & pd.to_numeric(eligible['open_interest'], errors='coerce').gt(0)
        ].copy()
        strikes = pd.to_numeric(eligible['strike_price'], errors='coerce')
        long_matches = eligible.loc[strikes.eq(long_strike)]
        short_matches = eligible.loc[strikes.eq(short_strike)]
        if len(long_matches) != 1 or len(short_matches) != 1:
            return result
        long_symbol = str(long_matches.iloc[0]['symbol']).strip().upper()
        short_symbol = str(short_matches.iloc[0]['symbol']).strip().upper()
        suffix = 'C' if option_type == 'call' else 'P'
        for symbol, strike in ((long_symbol, long_strike), (short_symbol, short_strike)):
            expected = f"{underlying}{expiry.strftime('%y%m%d')}{suffix}{int(round(strike * 1000)):08d}"
            if not _looks_like_occ_option_symbol(symbol) or symbol != expected or abs(strike * 1000 - round(strike * 1000)) > 1e-8:
                return result
        max_age = float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60'))
        max_spread = float(os.getenv('MAX_OPTION_SPREAD_PCT', '0.06'))
        if not math.isfinite(max_spread) or max_spread < 0 or option_client is None:
            raise ValueError('Option quote client or spread threshold unavailable')
        long_mid, long_spread, _ = option_snapshot_metrics(option_client, long_symbol, max_age)
        short_mid, short_spread, _ = option_snapshot_metrics(option_client, short_symbol, max_age)
        if long_spread > max_spread or short_spread > max_spread:
            result.update(rejection_code='REJECTED_SPREAD_WIDTH', rejection_detail='An option leg exceeds the spread-width limit.')
            return result
        debit = long_mid * (1 + long_spread / 2) - short_mid * (1 - short_spread / 2)
        if not math.isfinite(debit) or debit <= 0 or debit >= abs(short_strike - long_strike):
            result.update(rejection_code='REJECTED_SPREAD_DEBIT', rejection_detail='The indicative debit is invalid for this vertical.')
            return result
        limit_debit = Decimal(str(debit)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        max_risk = pd.to_numeric(row.get('max_risk_usd'), errors='coerce')
        allocation = pd.to_numeric(row.get('allocation_pct'), errors='coerce')
        if pd.isna(max_risk) or not math.isfinite(float(max_risk)) or float(max_risk) <= 0:
            result.update(rejection_code='REJECTED_RISK_BUDGET', rejection_detail='A positive spread risk budget is required.')
            return result
        allocation_budget = config.account_size_usd * float(allocation) / 100 if pd.notna(allocation) and float(allocation) > 0 else float(max_risk)
        risk_budget = min(float(max_risk), allocation_budget)
        if limit_debit <= 0 or not math.isfinite(risk_budget) or risk_budget <= 0:
            result.update(rejection_code='REJECTED_RISK_BUDGET', rejection_detail='The spread debit or available risk budget is invalid.')
            return result
        quantity = min(2, math.floor(risk_budget / (float(limit_debit) * 100)))
        if quantity <= 0:
            result.update(rejection_code='REJECTED_RISK_BUDGET', rejection_detail='One spread at the indicative debit exceeds the risk budget.')
            return result
        account_code, account_detail = validate_paper_spread_account(config, pd.Series({
            'limit_debit': float(limit_debit), 'proxy_qty': quantity, 'spread_risk_budget_usd': risk_budget,
        }))
        if account_code:
            result.update(rejection_code=account_code, rejection_detail=account_detail)
            return result
        guard_code, guard_detail, rank_source = _validate_debit_vertical_guardrails(
            row, config, underlying_midpoint, max(long_spread, short_spread), option_client,
        )
        if guard_code:
            result.update(rejection_code=guard_code, rejection_detail=guard_detail, iv_rank_source=rank_source)
            return result
        result.update(
            signal_close=row.get('signal_close', 0.0), bias=row.get('bias', 'neutral'),
            max_risk_usd=float(max_risk), allocation_pct=float(allocation) if pd.notna(allocation) else 0.0,
            options_setup=row.get('options_setup', ''),
            spread_type=spread_type, spread_option_type=option_type, spread_ratio_qty=1,
            long_leg_symbol=long_symbol, short_leg_symbol=short_symbol,
            long_leg_strike=long_strike, short_leg_strike=short_strike,
            long_leg_intent='buy_to_open', short_leg_intent='sell_to_open',
            long_leg_ratio_qty=1, short_leg_ratio_qty=1,
            long_leg_midpoint=long_mid, short_leg_midpoint=short_mid,
            long_leg_ask=long_mid * (1 + long_spread / 2),
            short_leg_bid=short_mid * (1 - short_spread / 2),
            long_leg_spread_pct=long_spread, short_leg_spread_pct=short_spread,
            expiration_date=expiry_text, days_to_expiration=days,
            indicative_debit=round(debit, 4),
            limit_debit=float(limit_debit), spread_risk_budget_usd=round(risk_budget, 2),
            estimated_max_loss_usd=round(float(limit_debit) * 100 * quantity, 2),
            iv_rank_source=rank_source,
            proxy_qty=quantity,
            rejection_code='', rejection_detail='',
        )
    except Exception:
        result.update(rejection_code='REJECTED_SAFETY_DATA_UNAVAILABLE', rejection_detail='Spread quote or metadata is unavailable or invalid.')
    return result


def prepare_option_execution_preview(
    queue: pd.DataFrame,
    config: TradingConfig,
    contracts_map: dict[str, pd.DataFrame] | None = None,
    min_days: int | None = None,
    max_days: int | None = None,
    top_n: int = 2,
    quote_client=None,
    option_quote_client=None,
) -> pd.DataFrame:
    if queue.empty:
        return pd.DataFrame()

    working = queue.copy()
    for col, default in {
        'approval_status': 'pending',
        'approved_for_submit': False,
        'order_status': 'queued_for_review',
        'last_submitted_at': '',
        'option_order_status': 'queued_for_review',
        'last_option_submitted_at': '',
    }.items():
        if col not in working.columns:
            working[col] = default

    min_days = int(min_days if min_days is not None else config.min_option_dte)
    max_days = int(max_days if max_days is not None else config.max_option_dte)
    min_days = max(1, min_days)
    max_days = max(min_days, max_days)

    preview_rows: list[dict[str, Any]] = []
    for _, row in working.iterrows():
        if len(preview_rows) >= top_n:
            break
        approval_status, approved_for_submit = _resolve_submit_approval(row, config)
        existing_status = str(row.get('option_order_status', 'queued_for_review')).strip().lower()
        already_submitted = _is_already_submitted(existing_status, row.get('last_option_submitted_at', ''))
        underlying = str(row.get('ticker', '')).upper()
        existing_rejection = str(row.get('rejection_code', '') or '').strip()
        if existing_rejection.startswith('REJECTED_'):
            preview_rows.append({
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'ticker': underlying, 'underlying_ticker': underlying, 'proxy_side': 'buy', 'proxy_qty': 0,
                'execution_mode': config.trading_mode,
                'execution_style': 'multi_leg_option_paper' if _is_spread_order(row) else 'single_leg_option_paper',
                'client_order_id': f'{config.trading_mode}-option-{underlying}-rejected',
                'approved_for_submit': False, 'ready_for_broker_submit': False,
                'rejection_code': existing_rejection, 'rejection_detail': str(row.get('rejection_detail', '')),
            })
            continue
        option_type, target_strike = _infer_option_type_and_strike(str(row.get('options_setup', '')), str(row.get('bias', 'neutral')))
        target_days = _resolve_target_days_from_row(row, default_days=(config.target_option_dte_min + config.target_option_dte_max) // 2)

        rejection_code = ''
        rejection_detail = ''
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from .market_data import stock_quote_midpoint

            if quote_client is None:
                if not config.api_key or not config.secret_key:
                    raise ValueError('Market-data credentials unavailable')
                quote_client = StockHistoricalDataClient(config.api_key, config.secret_key)
            midpoint = stock_quote_midpoint(quote_client, underlying, float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60')))
            drift = _intraday_drift(row.get('signal_close'), midpoint)
            max_drift = float(os.getenv('MAX_INTRADAY_DRIFT_PCT', '0.05'))
            if drift is None or not math.isfinite(max_drift) or max_drift < 0:
                raise ValueError('Signal close or drift threshold invalid')
            if abs(drift) > max_drift:
                rejection_code = 'REJECTED_INTRADAY_DRIFT'
                rejection_detail = f'Intraday drift {drift * 100:.2f}% exceeds {max_drift * 100:.2f}%.'
            target_strike = midpoint
        except Exception as exc:
            rejection_code = 'REJECTED_SAFETY_DATA_UNAVAILABLE'
            rejection_detail = f'Underlying quote unavailable or invalid ({type(exc).__name__}).'
        if rejection_code:
            preview_rows.append({
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'ticker': underlying, 'underlying_ticker': underlying, 'proxy_side': 'buy', 'proxy_qty': 0,
                'execution_mode': config.trading_mode, 'execution_style': 'single_leg_option_paper',
                'client_order_id': f'{config.trading_mode}-option-{underlying}-rejected',
                'approved_for_submit': False, 'ready_for_broker_submit': False,
                'rejection_code': rejection_code, 'rejection_detail': rejection_detail,
            })
            continue

        if _is_spread_order(row):
            raw_reconciliation_status = row.get('spread_reconciliation_status', '')
            reconciliation_status = str(raw_reconciliation_status).strip().lower() if pd.notna(raw_reconciliation_status) else ''
            raw_spread_order_id = row.get('spread_order_id', '')
            spread_order_id = str(raw_spread_order_id).strip() if pd.notna(raw_spread_order_id) else ''
            if reconciliation_status in {'unknown', 'mismatched'}:
                preview_rows.append({
                    'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                    'ticker': underlying, 'underlying_ticker': underlying, 'proxy_side': 'buy', 'proxy_qty': 0,
                    'execution_mode': config.trading_mode, 'execution_style': 'multi_leg_option_paper',
                    'client_order_id': spread_order_id,
                    'approved_for_submit': False, 'ready_for_broker_submit': False,
                    'rejection_code': 'REJECTED_SPREAD_RECONCILIATION',
                    'rejection_detail': 'Existing spread parent or leg state is ambiguous; manual reconciliation is required.',
                })
                continue
            if already_submitted or spread_order_id:
                preview_rows.append({
                    'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                    'ticker': underlying, 'underlying_ticker': underlying, 'proxy_side': 'buy', 'proxy_qty': 0,
                    'execution_mode': config.trading_mode, 'execution_style': 'multi_leg_option_paper',
                    'client_order_id': spread_order_id,
                    'approved_for_submit': False, 'ready_for_broker_submit': False,
                    'rejection_code': 'REJECTED_SPREAD_ORDER_EXISTS',
                    'rejection_detail': 'An existing spread order must be reconciled before another entry is considered.',
                })
                continue
            spread_contracts = contracts_map.get(underlying, pd.DataFrame()) if contracts_map is not None else fetch_option_contracts(
                config, underlying, min_days=21, max_days=45, target_strike=target_strike,
            )
            if option_quote_client is None and config.api_key and config.secret_key:
                from alpaca.data.historical import OptionHistoricalDataClient

                try:
                    option_quote_client = OptionHistoricalDataClient(config.api_key, config.secret_key)
                except Exception:
                    option_quote_client = None
            spread_preview = _preview_debit_vertical(row, config, spread_contracts, option_quote_client, midpoint)
            spread_preview.update(
                approval_status=approval_status, approved_for_submit=approved_for_submit,
                queue_order_status=existing_status,
                already_submitted=already_submitted,
                ready_for_broker_submit=bool(not spread_preview.get('rejection_code') and approved_for_submit and not already_submitted),
            )
            preview_rows.append(spread_preview)
            continue

        contracts = contracts_map.get(underlying, pd.DataFrame()) if contracts_map else fetch_option_contracts(
            config,
            underlying,
            min_days=min_days,
            max_days=max_days,
            target_strike=target_strike,
        )
        chosen = choose_best_option_contract(
            contracts,
            underlying_symbol=underlying,
            option_type=option_type,
            target_strike=target_strike,
            min_days=min_days,
            max_days=max_days,
            target_min_days=config.target_option_dte_min,
            target_max_days=config.target_option_dte_max,
            target_days=target_days,
        )
        if not chosen:
            continue

        chosen_strike = float(chosen.get('strike_price', 0.0) or 0.0)
        if _strike_mismatch_too_large(target_strike, chosen_strike):
            continue

        close_price = float(chosen.get('close_price', 0.0) or 0.0)
        estimated_contract_cost = max(close_price * 100, 1.0)
        target_notional = config.account_size_usd * (float(row.get('allocation_pct', 1.0) or 1.0) / 100.0)
        max_risk_usd = float(row.get('max_risk_usd', 0.0) or 0.0)
        risk_budget = target_notional
        if max_risk_usd > 0:
            risk_budget = min(risk_budget, max_risk_usd)
        qty = min(2, math.floor(risk_budget / estimated_contract_cost)) if estimated_contract_cost > 0 else 0
        if qty <= 0:
            continue
        client_order_id = f"{config.trading_mode}-option-{underlying}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        preview_rows.append({
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'ticker': str(chosen.get('symbol', underlying)),
            'underlying_ticker': underlying,
            'signal_close': row.get('signal_close', 0.0),
            'bias': row.get('bias', 'neutral'),
            'proxy_side': 'buy',
            'proxy_qty': int(qty),
            'signal_score': int(row.get('signal_score', 0)),
            'projected_return_pct': float(row.get('projected_return_pct', 0.0)),
            'max_risk_usd': max_risk_usd,
            'options_setup': row.get('options_setup', 'No trade'),
            'contract_name': chosen.get('name', ''),
            'expiration_date': str(chosen.get('expiration_date', ''))[:10],
            'days_to_expiration': int(chosen.get('days_to_expiration', 0) or 0),
            'strike_price': chosen_strike,
            'option_type': option_type,
            'estimated_contract_cost_usd': round(estimated_contract_cost, 2),
            'approval_status': approval_status,
            'approved_for_submit': approved_for_submit,
            'queue_order_status': existing_status,
            'already_submitted': already_submitted,
            'last_submitted_at': row.get('last_option_submitted_at', ''),
            'execution_mode': config.trading_mode,
            'execution_style': 'single_leg_option_paper',
            'client_order_id': client_order_id,
            'ready_for_broker_submit': bool(qty > 0 and approved_for_submit and not already_submitted),
        })

    return pd.DataFrame(preview_rows)


def prepare_equity_execution_preview(
    queue: pd.DataFrame,
    signals: pd.DataFrame,
    config: TradingConfig,
) -> pd.DataFrame:
    if queue.empty or signals.empty:
        return pd.DataFrame()

    if 'approval_status' not in queue.columns:
        queue['approval_status'] = 'pending'
    if 'approved_for_submit' not in queue.columns:
        queue['approved_for_submit'] = False
    if 'order_status' not in queue.columns:
        queue['order_status'] = 'queued_for_review'
    if 'last_submitted_at' not in queue.columns:
        queue['last_submitted_at'] = ''

    merged = queue.merge(signals[['ticker', 'last_close']], on='ticker', how='left')
    merged['last_close'] = merged['last_close'].fillna(0.0).astype(float)
    merged['allocation_pct'] = merged['allocation_pct'].astype(float).clip(upper=config.max_position_pct)

    preview_rows: list[dict[str, Any]] = []
    for _, row in merged.head(config.max_positions).iterrows():
        side = 'buy' if str(row.get('bias', '')).lower() == 'bullish' else 'sell'
        last_close = float(row.get('last_close', 0.0))
        allocation_pct = float(row.get('allocation_pct', 0.0))
        target_notional = config.account_size_usd * (allocation_pct / 100)
        qty = max(1, math.floor(target_notional / last_close)) if last_close > 0 else 0
        approval_status, approved_for_submit = _resolve_submit_approval(row, config)
        existing_status = str(row.get('order_status', 'queued_for_review')).strip().lower()
        already_submitted = _is_already_submitted(existing_status, row.get('last_submitted_at', ''))
        client_order_id = f"{config.trading_mode}-{row['ticker']}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        preview_rows.append(
            {
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'ticker': row['ticker'],
                'bias': row.get('bias', 'neutral'),
                'proxy_side': side,
                'last_close': round(last_close, 2),
                'target_notional_usd': round(target_notional, 2),
                'proxy_qty': int(qty),
                'signal_score': int(row.get('signal_score', 0)),
                'projected_return_pct': float(row.get('projected_return_pct', 0.0)),
                'max_risk_usd': float(row.get('max_risk_usd', 0.0)),
                'options_setup': row.get('options_setup', 'No trade'),
                'approval_status': approval_status,
                'approved_for_submit': approved_for_submit,
                'queue_order_status': existing_status,
                'already_submitted': already_submitted,
                'last_submitted_at': row.get('last_submitted_at', ''),
                'execution_mode': config.trading_mode,
                'execution_style': 'proxy_equity_paper' if config.use_proxy_equities else 'options_contract_pending',
                'client_order_id': client_order_id,
                'ready_for_broker_submit': bool(config.use_proxy_equities and qty > 0 and approved_for_submit and not already_submitted),
            }
        )

    return pd.DataFrame(preview_rows)


def save_execution_preview(preview: pd.DataFrame) -> Path | None:
    if preview.empty:
        return None
    path = OUTPUT_DIR / 'execution_preview.csv'
    preview.to_csv(path, index=False)
    return path


def append_execution_log(results: pd.DataFrame) -> Path | None:
    if results.empty:
        return None

    path = OUTPUT_DIR / 'execution_log.csv'
    if path.exists():
        existing = pd.read_csv(path)
        combined = pd.concat([existing, results], ignore_index=True)
        combined.to_csv(path, index=False)
    else:
        results.to_csv(path, index=False)
    return path


def run_execution_cycle(config: TradingConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    queue = load_latest_csv('paper_trade_queue')
    signals = load_latest_csv('latest_signals')
    preview = prepare_equity_execution_preview(queue, signals, config)
    results = submit_orders(preview, config)
    update_queue_after_execution(results)
    return preview, results


def run_option_execution_cycle(
    config: TradingConfig,
    min_days: int | None = None,
    max_days: int | None = None,
    top_n: int = 2,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    queue = load_latest_csv('paper_trade_queue')
    preview = prepare_option_execution_preview(queue, config, min_days=min_days, max_days=max_days, top_n=top_n)
    results = submit_orders(preview, config)
    update_queue_after_execution(results)
    return preview, results


def prepare_exit_execution_preview(
    recommendations: pd.DataFrame,
    positions: pd.DataFrame,
    config: TradingConfig,
    option_quote_client=None,
) -> pd.DataFrame:
    from .exit_manager import _paired_option_groups

    if recommendations.empty or positions.empty:
        return pd.DataFrame()

    working = recommendations.copy()
    for col, default in {
        'exit_approved': False,
        'order_status': 'monitor',
        'last_submitted_at': '',
        'exit_pct': 100.0,
    }.items():
        if col not in working.columns:
            working[col] = default

    position_columns = [
        column for column in (
            'symbol', 'side', 'qty', 'market_value', 'unrealized_pl',
            'early_assignment', 'early_assignment_flag', 'early_assignment_risk',
            'assignment_pending', 'assignment_flag', 'exercise_pending',
        ) if column in positions.columns
    ]
    merged = working.merge(
        positions[position_columns] if not positions.empty else pd.DataFrame(columns=['symbol', 'side', 'qty', 'market_value', 'unrealized_pl']),
        on='symbol',
        how='left',
        suffixes=('', '_position'),
    )

    exit_enabled = _bool_env('ENABLE_EXIT_AUTOMATION', False) and config.trading_mode == 'paper'
    preview_rows: list[dict[str, Any]] = []
    processed_spreads: set[str] = set()
    paired_position_groups = _paired_option_groups(positions)

    def append_manual_review_alert(
        row: pd.Series,
        paired_symbols: list[str],
        spread_key: str,
        detail: str,
    ) -> None:
        ticker = str(row.get('underlying_symbol', row.get('ticker', ''))).strip().upper()
        if not ticker or _looks_like_occ_option_symbol(ticker):
            symbol_match = re.match(r'^([A-Z]+)', str(row.get('symbol', '')))
            if symbol_match:
                ticker = symbol_match.group(1)
        preview_rows.append({
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'ticker': ticker,
            'underlying_ticker': ticker,
            'action': 'hold',
            'reason': f'MANUAL_REVIEW_REQUIRED: {detail}',
            'current_side': 'paired',
            'current_qty': 0,
            'exit_pct': 0,
            'exit_qty': 0,
            'exit_side': 'paired',
            'proxy_side': 'sell',
            'proxy_qty': 0,
            'approval_status': 'blocked',
            'approved_for_submit': False,
            'already_submitted': False,
            'execution_mode': config.trading_mode,
            'execution_style': 'protective_exit_mleg_paper',
            'client_order_id': '',
            'spread_lifecycle_status': 'MANUAL_REVIEW_REQUIRED',
            'spread_group_key': spread_key,
            'paired_symbols': ','.join(paired_symbols),
            'spread_alert_required': True,
            'spread_alert_detail': detail,
            'rejection_code': 'MANUAL_REVIEW_REQUIRED',
            'rejection_detail': detail,
            'ready_for_broker_submit': False,
        })

    for _, row in merged.iterrows():
        action = str(row.get('action', 'hold')).strip().lower()
        if action == 'hold':
            continue

        symbol = str(row.get('symbol', '')).upper()
        lifecycle = str(row.get('spread_lifecycle_status', '')).strip().lower()
        if lifecycle == 'manual_review_required' or _as_bool(row.get('spread_alert_required', False)):
            continue
        if lifecycle == 'healthy_matched':
            paired_symbols = sorted({
                item.strip().upper()
                for item in str(row.get('paired_symbols', '')).split(',')
                if item.strip()
            })
            spread_key = str(row.get('spread_group_key', '') or ','.join(paired_symbols))
            if spread_key in processed_spreads:
                continue
            processed_spreads.add(spread_key)
            if len(paired_symbols) != 2:
                append_manual_review_alert(row, paired_symbols, spread_key, 'Spread leg list is incomplete or ambiguous.')
                continue
            paired_positions = positions[
                positions['symbol'].astype(str).str.strip().str.upper().isin(paired_symbols)
            ].copy()
            if len(paired_positions) != 2 or set(paired_positions['symbol'].astype(str).str.strip().str.upper()) != set(paired_symbols):
                append_manual_review_alert(row, paired_symbols, spread_key, 'A spread leg is missing from current positions.')
                continue
            side_map = {
                str(position.get('symbol', '')).strip().upper(): str(position.get('side', '')).strip().lower()
                for _, position in paired_positions.iterrows()
            }
            qty_values = pd.to_numeric(paired_positions.get('qty', pd.Series(dtype=float)), errors='coerce')
            flagged = any(
                _is_assignment_flagged(position.get(column, False))
                for _, position in paired_positions.iterrows()
                for column in ('early_assignment', 'early_assignment_flag', 'early_assignment_risk',
                               'assignment_pending', 'assignment_flag', 'exercise_pending')
            )
            if set(side_map.values()) != {'long', 'short'} or qty_values.isna().any() or qty_values.nunique() != 1 or flagged:
                detail = 'Early assignment or exercise requires manual review.' if flagged else 'Spread leg quantities or sides are mismatched.'
                append_manual_review_alert(row, paired_symbols, spread_key, detail)
                continue
            paired_recommendations = working[
                working['symbol'].astype(str).str.strip().str.upper().isin(paired_symbols)
            ]
            if len(paired_recommendations) != 2 or not paired_recommendations['spread_lifecycle_status'].astype(str).str.lower().eq('healthy_matched').all():
                append_manual_review_alert(row, paired_symbols, spread_key, 'Paired exit recommendations are incomplete or inconsistent.')
                continue
            exit_pct = min(
                max(float(value or 0.0), 0.0)
                for value in paired_recommendations['exit_pct'].tolist()
            )
            exit_qty = max(1, math.ceil(float(qty_values.iloc[0]) * (exit_pct / 100.0)))
            approved = all(_as_bool(value) for value in paired_recommendations['exit_approved'].tolist())
            existing_statuses = paired_recommendations['order_status'].astype(str).str.strip().str.lower()
            already_submitted = any(
                _is_already_submitted(status, submitted_at)
                for status, submitted_at in zip(existing_statuses, paired_recommendations['last_submitted_at'])
            )
            long_symbol = next(leg for leg, side in side_map.items() if side == 'long')
            short_symbol = next(leg for leg, side in side_map.items() if side == 'short')
            underlying = str(row.get('underlying_symbol', '')).strip().upper()
            digest = hashlib.sha256('|'.join(paired_symbols).encode('utf-8')).hexdigest()[:16]
            client_order_id = f'{config.trading_mode}-exit-spread-{underlying}-{digest}'
            preview: dict[str, Any] = {
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'ticker': underlying, 'underlying_ticker': underlying,
                'action': action, 'reason': str(row.get('reason', '')),
                'current_side': 'paired', 'current_qty': float(qty_values.iloc[0]),
                'exit_pct': exit_pct, 'exit_qty': int(exit_qty),
                'exit_side': 'paired', 'proxy_side': 'sell', 'proxy_qty': int(exit_qty),
                'approval_status': 'approved' if approved else 'pending',
                'approved_for_submit': approved, 'queue_order_status': '|'.join(existing_statuses),
                'already_submitted': already_submitted,
                'last_submitted_at': max(
                    (str(value) for value in paired_recommendations['last_submitted_at']),
                    default='',
                ),
                'execution_mode': config.trading_mode,
                'execution_style': 'protective_exit_mleg_paper',
                'client_order_id': client_order_id,
                'spread_lifecycle_status': 'healthy_matched',
                'spread_group_key': spread_key,
                'paired_symbols': ','.join(paired_symbols),
                'long_leg_symbol': long_symbol, 'short_leg_symbol': short_symbol,
                'long_leg_intent': 'sell_to_close', 'short_leg_intent': 'buy_to_close',
                'long_leg_ratio_qty': 1, 'short_leg_ratio_qty': 1,
                'rejection_code': '', 'rejection_detail': '',
                'ready_for_broker_submit': False,
            }
            code, detail, _, limit_price = _fresh_vertical_close_request(
                pd.Series({**preview, 'client_order_id': client_order_id}), config, option_quote_client,
            )
            if code:
                preview.update(rejection_code=code, rejection_detail=detail)
            else:
                preview['limit_price'] = float(limit_price)
                preview['ready_for_broker_submit'] = bool(
                    exit_enabled and approved and not already_submitted and exit_qty > 0
                )
            preview_rows.append(preview)
            continue
        inferred_pair = paired_position_groups.get(symbol)
        if inferred_pair:
            group_key, symbols_text = inferred_pair
            if group_key in processed_spreads:
                continue
            processed_spreads.add(group_key)
            inferred_symbols = symbols_text.split(',')
            append_manual_review_alert(
                row, inferred_symbols, group_key,
                'Option positions are paired but the recommendation is not marked healthy and matched.',
            )
            continue

        side = str(row.get('side', row.get('side_position', ''))).strip().lower()
        qty = float(row.get('qty', row.get('qty_position', 0.0)) or 0.0)
        exit_pct = min(max(float(row.get('exit_pct', 100.0) or 100.0), 0.0), 100.0)
        exit_qty = max(1, math.ceil(qty * (exit_pct / 100.0))) if qty > 0 else 0
        exit_side = 'sell' if side == 'long' else 'buy'
        approved = _as_bool(row.get('exit_approved', False))
        existing_status = str(row.get('order_status', 'monitor')).strip().lower()
        already_submitted = _is_already_submitted(existing_status, row.get('last_submitted_at', ''))
        client_order_id = f"{config.trading_mode}-exit-{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        preview_rows.append({
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'ticker': symbol,
            'action': action,
            'reason': str(row.get('reason', 'No exit reason provided.')),
            'current_side': side,
            'current_qty': qty,
            'exit_pct': exit_pct,
            'exit_qty': int(exit_qty),
            'exit_side': exit_side,
            'proxy_side': exit_side,
            'proxy_qty': int(exit_qty),
            'approval_status': 'approved' if approved else 'pending',
            'approved_for_submit': approved,
            'queue_order_status': existing_status,
            'already_submitted': already_submitted,
            'last_submitted_at': row.get('last_submitted_at', ''),
            'execution_mode': config.trading_mode,
            'execution_style': 'protective_exit_paper',
            'client_order_id': client_order_id,
            'ready_for_broker_submit': bool(exit_enabled and approved and not already_submitted and exit_qty > 0),
        })

    return pd.DataFrame(preview_rows)


def save_exit_execution_preview(preview: pd.DataFrame) -> Path | None:
    if preview.empty:
        return None
    path = OUTPUT_DIR / 'exit_execution_preview.csv'
    preview.to_csv(path, index=False)
    return path


def run_exit_execution_cycle(config: TradingConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    recommendations = load_latest_csv('exit_recommendations')
    positions = load_latest_csv('broker_positions')
    preview = prepare_exit_execution_preview(recommendations, positions, config)
    sent_groups: set[str] = set()
    for source in (recommendations, preview):
        if source.empty:
            continue
        alerts = source[
            source.get('spread_alert_required', pd.Series(False, index=source.index)).map(_as_bool)
            | source.get('spread_lifecycle_status', pd.Series('', index=source.index))
            .astype(str).str.upper().eq('MANUAL_REVIEW_REQUIRED')
        ]
        for _, alert in alerts.iterrows():
            group = str(alert.get('spread_group_key', alert.get('symbol', ''))).strip()
            if group in sent_groups:
                continue
            sent_groups.add(group)
            detail = str(alert.get('spread_alert_detail', alert.get('reason', 'Spread requires manual review.')))
            print(f'MANUAL_REVIEW_REQUIRED for spread {group}: {detail}')
            if config.discord_webhook_url:
                notify_discord(
                    config.discord_webhook_url,
                    'Manual Review Required: Option Spread',
                    {'Spread': str(alert.get('paired_symbols', alert.get('symbol', 'unknown'))), 'Reason': detail},
                    color=15548997,
                    status='rejected',
                )
    submit_preview = preview
    if not preview.empty and 'spread_lifecycle_status' in preview.columns:
        submit_preview = preview[
            ~preview['spread_lifecycle_status'].astype(str).str.upper().eq('MANUAL_REVIEW_REQUIRED')
        ]
    results = submit_orders(submit_preview, config)
    update_exit_recommendations_after_execution(results)
    return preview, results


def submit_orders(preview: pd.DataFrame, config: TradingConfig) -> pd.DataFrame:
    if preview.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for _, order in preview.iterrows():
        rows.append(submit_single_order(order, config))
    return pd.DataFrame(rows)


def update_queue_after_execution(results: pd.DataFrame) -> None:
    queue_path = OUTPUT_DIR / 'paper_trade_queue.csv'
    if results.empty or not queue_path.exists():
        return

    queue = pd.read_csv(queue_path)
    if 'order_status' not in queue.columns:
        queue['order_status'] = 'queued_for_review'
    if 'last_submitted_at' not in queue.columns:
        queue['last_submitted_at'] = ''
    if 'option_order_status' not in queue.columns:
        queue['option_order_status'] = 'queued_for_review'
    if 'last_option_submitted_at' not in queue.columns:
        queue['last_option_submitted_at'] = ''
    queue['order_status'] = queue['order_status'].fillna('queued_for_review').astype(str)
    queue['last_submitted_at'] = queue['last_submitted_at'].fillna('').astype(str)
    queue['option_order_status'] = queue['option_order_status'].fillna('queued_for_review').astype(str)
    queue['last_option_submitted_at'] = queue['last_option_submitted_at'].fillna('').astype(str)
    if 'rejection_code' not in queue.columns:
        queue['rejection_code'] = ''
    if 'rejection_detail' not in queue.columns:
        queue['rejection_detail'] = ''
    queue['rejection_code'] = queue['rejection_code'].fillna('').astype(object)
    queue['rejection_detail'] = queue['rejection_detail'].fillna('').astype(object)

    for _, result in results.iterrows():
        ticker = str(result.get('underlying_ticker', result.get('ticker', ''))).upper()
        status = str(result.get('status', '')).strip().lower()
        submitted_at = result.get('submitted_at_utc', '')
        execution_style = str(result.get('execution_style', ''))
        mask = queue['ticker'].astype(str).str.upper() == ticker
        if not mask.any():
            continue

        status_col = 'option_order_status' if 'option' in execution_style else 'order_status'
        time_col = 'last_option_submitted_at' if 'option' in execution_style else 'last_submitted_at'

        if status in {'accepted', 'new', 'filled', 'partially_filled', 'pending_new'}:
            queue.loc[mask, status_col] = status
            queue.loc[mask, time_col] = submitted_at
        elif status == 'preview_only':
            queue.loc[mask, status_col] = 'approved_preview_only'
        elif status == 'blocked' and str(result.get('rejection_code', '')).strip():
            queue.loc[mask, status_col] = 'blocked'
            queue.loc[mask, 'rejection_code'] = str(result.get('rejection_code', ''))
            queue.loc[mask, 'rejection_detail'] = str(result.get('detail', ''))

    queue.to_csv(queue_path, index=False)


def _broker_headers(config: TradingConfig) -> dict[str, str]:
    return {
        'APCA-API-KEY-ID': config.api_key,
        'APCA-API-SECRET-KEY': config.secret_key,
        'Content-Type': 'application/json',
    }


def _fetch_alpaca_json(config: TradingConfig, endpoint: str) -> Any:
    try:
        response = _request_with_retries(
            method='GET',
            url=f"{config.base_url}{endpoint}",
            headers=_broker_headers(config),
            timeout=20,
        )
        return response.json()
    except requests.HTTPError as exc:
        raise requests.HTTPError(_describe_broker_auth_failure(config, endpoint, exc)) from exc


def sync_broker_state(config: TradingConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    timestamp = datetime.now(timezone.utc).isoformat()
    account_columns = [
        'synced_at_utc', 'broker', 'mode', 'connection_status', 'market_status',
        'equity', 'cash', 'buying_power', 'portfolio_value',
        'options_approved_level', 'options_trading_level', 'options_buying_power',
        'detail'
    ]
    position_columns = [
        'synced_at_utc', 'symbol', 'side', 'qty', 'market_value', 'unrealized_pl',
        'early_assignment', 'early_assignment_flag', 'early_assignment_risk',
        'assignment_pending', 'assignment_flag', 'exercise_pending',
    ]
    order_columns = [
        'synced_at_utc', 'order_id', 'parent_order_id', 'symbol', 'underlying_symbol', 'side', 'qty',
        'status', 'submitted_at', 'client_order_id', 'order_class', 'filled_qty', 'filled_avg_price',
        'legs_json', 'spread_reconciliation_status',
    ]

    if config.broker != 'alpaca':
        account = pd.DataFrame([{
            'synced_at_utc': timestamp,
            'broker': config.broker,
            'mode': config.trading_mode,
            'connection_status': 'unsupported_broker',
            'market_status': 'unknown',
            'equity': 0.0,
            'cash': 0.0,
            'buying_power': 0.0,
            'portfolio_value': 0.0,
            'options_approved_level': 0,
            'options_trading_level': 0,
            'options_buying_power': 0.0,
            'detail': f'Broker {config.broker} is not supported yet.',
        }], columns=account_columns)
        positions = pd.DataFrame(columns=position_columns)
        orders = pd.DataFrame(columns=order_columns)
        _save_broker_snapshots(account, positions, orders)
        return account, positions

    if not config.api_key or not config.secret_key:
        account = pd.DataFrame([{
            'synced_at_utc': timestamp,
            'broker': config.broker,
            'mode': config.trading_mode,
            'connection_status': 'missing_credentials',
            'market_status': 'unknown',
            'equity': 0.0,
            'cash': 0.0,
            'buying_power': 0.0,
            'portfolio_value': 0.0,
            'options_approved_level': 0,
            'options_trading_level': 0,
            'options_buying_power': 0.0,
            'detail': 'Add Alpaca paper credentials to the project .env file to enable broker sync.',
        }], columns=account_columns)
        positions = pd.DataFrame(columns=position_columns)
        orders = pd.DataFrame(columns=order_columns)
        _save_broker_snapshots(account, positions, orders)
        return account, positions

    try:
        account_body = _fetch_alpaca_json(config, '/v2/account')
        clock_body = _fetch_alpaca_json(config, '/v2/clock')
        account = pd.DataFrame([{
            'synced_at_utc': timestamp,
            'broker': config.broker,
            'mode': config.trading_mode,
            'connection_status': 'connected',
            'market_status': 'open' if bool(clock_body.get('is_open')) else 'closed',
            'equity': float(account_body.get('equity', 0.0) or 0.0),
            'cash': float(account_body.get('cash', 0.0) or 0.0),
            'buying_power': float(account_body.get('buying_power', 0.0) or 0.0),
            'portfolio_value': float(account_body.get('portfolio_value', 0.0) or 0.0),
            'options_approved_level': int(account_body.get('options_approved_level', 0) or 0),
            'options_trading_level': int(account_body.get('options_trading_level', 0) or 0),
            'options_buying_power': float(account_body.get('options_buying_power', 0.0) or 0.0),
            'detail': str(account_body.get('status', 'active')),
        }], columns=account_columns)
    except Exception as exc:
        detail = _describe_broker_auth_failure(config, '/v2/account', exc)
        connection_status = _classify_connection_status(detail)
        account = pd.DataFrame([{
            'synced_at_utc': timestamp,
            'broker': config.broker,
            'mode': config.trading_mode,
            'connection_status': connection_status,
            'market_status': 'unknown',
            'equity': 0.0,
            'cash': 0.0,
            'buying_power': 0.0,
            'portfolio_value': 0.0,
            'options_approved_level': 0,
            'options_trading_level': 0,
            'options_buying_power': 0.0,
            'detail': detail,
        }], columns=account_columns)

    try:
        positions_body = _fetch_alpaca_json(config, '/v2/positions')
        positions_rows = []
        for item in positions_body:
            positions_rows.append({
                'synced_at_utc': timestamp,
                'symbol': item.get('symbol', ''),
                'side': item.get('side', ''),
                'qty': float(item.get('qty', 0.0) or 0.0),
                'market_value': float(item.get('market_value', 0.0) or 0.0),
                'unrealized_pl': float(item.get('unrealized_pl', 0.0) or 0.0),
                'early_assignment': item.get('early_assignment', False),
                'early_assignment_flag': item.get('early_assignment_flag', False),
                'early_assignment_risk': item.get('early_assignment_risk', False),
                'assignment_pending': item.get('assignment_pending', False),
                'assignment_flag': item.get('assignment_flag', False),
                'exercise_pending': item.get('exercise_pending', False),
            })
        positions = pd.DataFrame(positions_rows, columns=position_columns)
    except Exception:
        positions = pd.DataFrame(columns=position_columns)

    try:
        orders_body = _fetch_alpaca_json(config, '/v2/orders?status=all&limit=500&direction=desc&nested=true')
        order_rows = []
        for item in orders_body:
            legs = item.get('legs') if isinstance(item.get('legs'), list) else []
            is_multileg = str(item.get('order_class', '')).strip().lower() == 'mleg'
            valid_legs = [leg for leg in legs if isinstance(leg, dict)]
            leg_fills = [pd.to_numeric(leg.get('filled_qty'), errors='coerce') for leg in valid_legs]
            ratios = [pd.to_numeric(leg.get('ratio_qty'), errors='coerce') for leg in valid_legs]
            leg_symbols = [str(leg.get('symbol', '')).strip().upper() for leg in valid_legs]
            leg_matches = [re.fullmatch(r'([A-Z]{1,6})(\d{6})([CP])(\d{8})', symbol) for symbol in leg_symbols]
            parent_filled = pd.to_numeric(item.get('filled_qty'), errors='coerce')
            if not is_multileg:
                reconciliation = 'not_multileg'
            elif (
                len(legs) != 2 or len(leg_fills) != 2 or any(pd.isna(value) for value in leg_fills)
                or len(ratios) != 2 or any(pd.isna(value) or float(value) != 1 for value in ratios)
                or len(leg_matches) != 2 or any(match is None for match in leg_matches)
                or (leg_matches[0] and leg_matches[1] and (
                    leg_matches[0].groups()[:3] != leg_matches[1].groups()[:3]
                    or leg_matches[0].group(4) == leg_matches[1].group(4)
                ))
                or pd.isna(parent_filled)
            ):
                reconciliation = 'unknown'
            elif (
                abs(float(leg_fills[0]) - float(leg_fills[1])) > 1e-9
                or abs(float(leg_fills[0]) - float(parent_filled)) > 1e-9
            ):
                reconciliation = 'mismatched'
            else:
                reconciliation = 'matched'
            order_rows.append({
                'synced_at_utc': timestamp,
                'order_id': item.get('id', ''),
                'parent_order_id': item.get('parent_order_id', ''),
                'symbol': item.get('symbol', ''),
                'underlying_symbol': item.get('underlying_symbol', ''),
                'side': item.get('side', ''),
                'qty': float(item.get('qty', 0.0) or 0.0),
                'status': item.get('status', ''),
                'submitted_at': item.get('submitted_at', ''),
                'client_order_id': item.get('client_order_id', ''),
                'order_class': item.get('order_class', ''),
                'filled_qty': float(parent_filled) if pd.notna(parent_filled) else '',
                'filled_avg_price': item.get('filled_avg_price', ''),
                'legs_json': json.dumps(legs, sort_keys=True, separators=(',', ':')),
                'spread_reconciliation_status': reconciliation,
            })
        orders = pd.DataFrame(order_rows, columns=order_columns)
    except Exception:
        orders = pd.DataFrame(columns=order_columns)

    _save_broker_snapshots(account, positions, orders)
    update_queue_from_broker_orders(orders)
    update_exit_recommendations_from_broker_orders(orders)
    return account, positions


def _extract_underlying_from_client_order_id(client_order_id: Any) -> str:
    import re
    text = str(client_order_id or '')
    match = re.search(r'-(?:option|spread|exit(?:-spread)?)-([A-Z]+)-', text)
    if match:
        return match.group(1).upper()
    match = re.search(r'-(?:paper|live)-?([A-Z]+)-', text)
    if match:
        return match.group(1).upper()
    return ''


def update_queue_from_broker_orders(orders: pd.DataFrame) -> None:
    queue_path = OUTPUT_DIR / 'paper_trade_queue.csv'
    if orders.empty or not queue_path.exists():
        return

    queue = pd.read_csv(queue_path)
    if 'order_status' not in queue.columns:
        queue['order_status'] = 'queued_for_review'
    if 'last_submitted_at' not in queue.columns:
        queue['last_submitted_at'] = ''
    if 'option_order_status' not in queue.columns:
        queue['option_order_status'] = 'queued_for_review'
    if 'last_option_submitted_at' not in queue.columns:
        queue['last_option_submitted_at'] = ''
    queue['order_status'] = queue['order_status'].fillna('queued_for_review').astype(str)
    queue['last_submitted_at'] = queue['last_submitted_at'].fillna('').astype(str)
    queue['option_order_status'] = queue['option_order_status'].fillna('queued_for_review').astype(str)
    queue['last_option_submitted_at'] = queue['last_option_submitted_at'].fillna('').astype(str)

    for _, order in orders.iterrows():
        ticker = str(order.get('underlying_symbol', order.get('symbol', ''))).upper()
        status = str(order.get('status', '')).strip().lower()
        submitted_at = order.get('submitted_at', '')
        client_order_id = order.get('client_order_id', '')
        mask = queue['ticker'].astype(str).str.upper() == ticker
        if not mask.any():
            fallback_ticker = _extract_underlying_from_client_order_id(client_order_id)
            if fallback_ticker:
                mask = queue['ticker'].astype(str).str.upper() == fallback_ticker
        if not mask.any():
            continue

        is_option_order = '-option-' in str(client_order_id) or '-spread-' in str(client_order_id)
        status_col = 'option_order_status' if is_option_order else 'order_status'
        time_col = 'last_option_submitted_at' if is_option_order else 'last_submitted_at'
        queue.loc[mask, status_col] = status
        queue.loc[mask, time_col] = submitted_at
        if '-spread-' in str(client_order_id):
            for column, value in {
                'spread_order_id': order.get('order_id', ''),
                'spread_parent_status': status,
                'spread_filled_qty': order.get('filled_qty', 0),
                'spread_legs_json': order.get('legs_json', '[]'),
                'spread_reconciliation_status': order.get('spread_reconciliation_status', 'unknown'),
            }.items():
                if column not in queue.columns:
                    queue[column] = pd.Series('', index=queue.index, dtype=object)
                else:
                    queue[column] = queue[column].astype(object)
                queue.loc[mask, column] = str(value or '')

    queue.to_csv(queue_path, index=False)


def update_exit_recommendations_after_execution(results: pd.DataFrame) -> None:
    exit_path = OUTPUT_DIR / 'exit_recommendations.csv'
    if results.empty or not exit_path.exists():
        return

    recommendations = pd.read_csv(exit_path)
    if 'order_status' not in recommendations.columns:
        recommendations['order_status'] = 'monitor'
    if 'last_submitted_at' not in recommendations.columns:
        recommendations['last_submitted_at'] = ''
    # ensure string columns stay object dtype so datetime strings can be written
    recommendations['last_submitted_at'] = recommendations['last_submitted_at'].astype(object)
    recommendations['order_status'] = recommendations['order_status'].astype(object)

    for _, result in results.iterrows():
        ticker = str(result.get('ticker', '')).upper()
        paired_symbols = {
            symbol.strip().upper()
            for symbol in str(result.get('paired_symbols', '')).split(',')
            if symbol.strip()
        }
        status = str(result.get('status', '')).strip().lower()
        submitted_at = str(result.get('submitted_at_utc', ''))
        mask = recommendations['symbol'].astype(str).str.upper().eq(ticker)
        if paired_symbols:
            mask |= recommendations['symbol'].astype(str).str.upper().isin(paired_symbols)
        if not mask.any():
            continue
        recommendations.loc[mask, 'order_status'] = status
        recommendations.loc[mask, 'last_submitted_at'] = submitted_at

    recommendations.to_csv(exit_path, index=False)


def update_exit_recommendations_from_broker_orders(orders: pd.DataFrame) -> None:
    exit_path = OUTPUT_DIR / 'exit_recommendations.csv'
    if orders.empty or not exit_path.exists():
        return

    recommendations = pd.read_csv(exit_path)
    if recommendations.empty or 'symbol' not in recommendations.columns:
        return
    if 'order_status' not in recommendations.columns:
        recommendations['order_status'] = 'monitor'
    if 'last_submitted_at' not in recommendations.columns:
        recommendations['last_submitted_at'] = ''
    recommendations['order_status'] = recommendations['order_status'].astype(object)
    recommendations['last_submitted_at'] = recommendations['last_submitted_at'].astype(object)

    exit_orders = orders.copy()
    if 'client_order_id' in exit_orders.columns:
        exit_orders = exit_orders[exit_orders['client_order_id'].astype(str).str.contains('-exit-', case=False, na=False)]

    for _, order in exit_orders.iterrows():
        ticker = str(order.get('symbol', '')).upper()
        symbols = {ticker} if ticker else set()
        try:
            legs = json.loads(str(order.get('legs_json', '[]') or '[]'))
        except (json.JSONDecodeError, TypeError):
            legs = []
        symbols.update(
            str(leg.get('symbol', '')).strip().upper()
            for leg in legs if isinstance(leg, dict) and leg.get('symbol')
        )
        status = str(order.get('status', '')).strip().lower()
        submitted_at = order.get('submitted_at', '')
        mask = recommendations['symbol'].astype(str).str.upper().isin(symbols)
        if not mask.any():
            continue
        recommendations.loc[mask, 'order_status'] = status
        recommendations.loc[mask, 'last_submitted_at'] = submitted_at

    recommendations.to_csv(exit_path, index=False)


def _save_broker_snapshots(account: pd.DataFrame, positions: pd.DataFrame, orders: pd.DataFrame) -> None:
    account.to_csv(OUTPUT_DIR / 'broker_account_status.csv', index=False)
    positions.to_csv(OUTPUT_DIR / 'broker_positions.csv', index=False)
    orders.to_csv(OUTPUT_DIR / 'broker_orders.csv', index=False)


def validate_live_option_entry(order: pd.Series, config: TradingConfig) -> tuple[str, str, str]:
    from zoneinfo import ZoneInfo

    from alpaca.data.historical import NewsClient, OptionHistoricalDataClient, StockHistoricalDataClient

    from .catalyst_events import count_recent_headlines, get_earnings_dates
    from .iv_history import calculate_iv_rank, realized_volatility_proxy_rank, safety_database_path
    from .market_data import fetch_history, option_snapshot_metrics, stock_quote_midpoint
    from .option_guardrails import evaluate_entry

    ticker = str(order.get('underlying_ticker', '')).strip().upper()
    contract = str(order.get('ticker', '')).strip().upper()
    if _is_spread_order(order):
        return 'REJECTED_MULTI_LEG_UNSUPPORTED', 'Spread setups cannot be submitted as a single option leg.', ''
    if not ticker or not config.api_key or not config.secret_key:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Market-data credentials or underlying ticker are missing.', ''
    try:
        max_age = float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60'))
        max_drift = float(os.getenv('MAX_INTRADAY_DRIFT_PCT', '0.05'))
        max_spread = float(os.getenv('MAX_OPTION_SPREAD_PCT', '0.06'))
        max_iv_rank = float(os.getenv('MAX_SINGLE_LEG_IV_RANK', '80.0'))
        blackout_days = int(os.getenv('EARNINGS_BLACKOUT_DAYS', '7'))
        news_threshold = int(os.getenv('BREAKING_NEWS_COUNT_THRESHOLD', '3'))
        news_move = float(os.getenv('BREAKING_NEWS_MOVE_PCT', '0.04'))
        reference = float(order.get('signal_close', 0))
        if not math.isfinite(reference) or reference <= 0:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Valid signal reference close is missing.', ''
        stock = StockHistoricalDataClient(config.api_key, config.secret_key)
        midpoint = stock_quote_midpoint(stock, ticker, max_age)
        drift = _intraday_drift(reference, midpoint)
        if drift is None:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Valid signal reference close is missing.', ''
        if not math.isfinite(max_drift) or max_drift < 0:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Invalid drift threshold.', ''
        if abs(drift) > max_drift:
            return 'REJECTED_INTRADAY_DRIFT', f'Intraday drift {drift * 100:.2f}% exceeds {max_drift * 100:.2f}%.', ''
        option = OptionHistoricalDataClient(config.api_key, config.secret_key)
        try:
            option_midpoint, spread, _ = option_snapshot_metrics(option, contract, max_age)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            if 'implied volatility' in str(exc).lower():
                return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Current option IV is missing or invalid.', ''
            return 'REJECTED_SPREAD_WIDTH', f'Option quote unavailable or invalid ({type(exc).__name__}).', ''
        if not math.isfinite(max_spread) or max_spread < 0:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'Invalid spread threshold.', ''
        if spread > max_spread:
            return 'REJECTED_SPREAD_WIDTH', f'Option spread {spread * 100:.2f}% exceeds {max_spread * 100:.2f}%.', ''
        risk_budget = float(order.get('max_risk_usd', 0))
        quantity = int(order.get('proxy_qty', 0))
        quoted_cost = option_midpoint * (1 + spread / 2) * 100 * quantity
        if not math.isfinite(risk_budget) or risk_budget <= 0 or quantity <= 0:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'A positive option risk budget and quantity are required.', ''
        if quoted_cost > risk_budget:
            return 'REJECTED_RISK_BUDGET', f'Quoted ask cost ${quoted_cost:.2f} exceeds risk budget ${risk_budget:.2f}.', ''
        now = datetime.now(timezone.utc)
        today = now.astimezone(ZoneInfo('America/New_York')).date()
        database = safety_database_path(PROJECT_DIR)
        earnings = get_earnings_dates(database, ticker, os.getenv('FINNHUB_API_KEY', ''), today, blackout_days)
        headlines = count_recent_headlines(NewsClient(config.api_key, config.secret_key), ticker, now, news_threshold)
        catalyst_decision = evaluate_entry(reference, midpoint, spread, earnings, headlines, 0.0, today,
                                          max_drift, max_spread, blackout_days, news_threshold, news_move, max_iv_rank)
        if catalyst_decision.code:
            return catalyst_decision.code, catalyst_decision.detail, ''
        atm_contracts = fetch_option_contracts(config, ticker, min_days=25, max_days=35, target_strike=midpoint)
        atm_contract = choose_best_option_contract(atm_contracts, ticker, 'call', midpoint, min_days=25, max_days=35,
                                                   target_min_days=25, target_max_days=35, target_days=30)
        atm_strike = float(atm_contract.get('strike_price', 0)) if atm_contract else 0.0
        if not math.isfinite(atm_strike) or atm_strike <= 0 or abs(atm_strike - midpoint) / midpoint > 0.05:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', 'A comparable 30-DTE ATM call is unavailable.', ''
        _, _, atm_iv = option_snapshot_metrics(option, str(atm_contract['symbol']), max_age)
        rank, observations = calculate_iv_rank(database, ticker, atm_iv, today)
        rank_source = 'iv_history'
        if rank is None and observations < 20 and not _bool_env('REQUIRE_STRICT_IV_RANK', True):
            history = fetch_history([ticker], period='2y', interval='1d').get(ticker, pd.DataFrame())
            rank = realized_volatility_proxy_rank(history['Close']) if 'Close' in history.columns else None
            rank_source = 'realized_volatility_proxy'
        if rank is None:
            return 'REJECTED_SAFETY_DATA_UNAVAILABLE', f'IV rank unavailable ({observations} historical observations).', rank_source
        decision = evaluate_entry(reference, midpoint, spread, earnings, headlines, rank, today,
                                  max_drift, max_spread, blackout_days, news_threshold, news_move, max_iv_rank)
        detail = decision.detail
        if rank_source == 'realized_volatility_proxy' and decision.code == 'REJECTED_IV_RANK':
            detail = detail.replace('IV rank', 'Realized-volatility proxy rank', 1)
        return decision.code, detail, rank_source
    except Exception as exc:
        return 'REJECTED_SAFETY_DATA_UNAVAILABLE', f'Safety data unavailable ({type(exc).__name__}).', ''


def submit_single_order(order: pd.Series, config: TradingConfig) -> dict[str, Any]:
    base_result = {
        'submitted_at_utc': datetime.now(timezone.utc).isoformat(),
        'ticker': order['ticker'],
        'underlying_ticker': order.get('underlying_ticker', order.get('ticker')),
        'proxy_side': order['proxy_side'],
        'proxy_qty': int(order['proxy_qty']),
        'execution_mode': config.trading_mode,
        'execution_style': order.get('execution_style', ''),
        'broker': config.broker,
        'client_order_id': order['client_order_id'],
    }

    def _notify_and_return(result: dict[str, Any]) -> dict[str, Any]:
        if config.discord_webhook_url:
            title, fields, color, status = _build_order_notification(order, result)
            if fields:
                notify_discord(config.discord_webhook_url, title, fields, color, status)
        return result

    preview_rejection = str(order.get('rejection_code', '') or '').strip()
    if preview_rejection and preview_rejection.lower() != 'nan':
        return _notify_and_return({**base_result, 'status': 'blocked', 'rejection_code': preview_rejection,
                                   'detail': str(order.get('rejection_detail', 'Safety validation rejected the setup.'))})

    if _is_spread_order(order):
        if 'protective_exit_mleg' in str(order.get('execution_style', '')).lower():
            return _notify_and_return(_submit_paper_vertical_close(order, config, base_result))
        return _notify_and_return(_submit_paper_debit_vertical(order, config, base_result))

    if _as_bool(order.get('already_submitted', False)):
        return _notify_and_return({
            **base_result,
            'status': 'skipped_existing',
            'detail': 'Order for this ticker was already submitted previously and was skipped.',
        })

    if not _as_bool(order.get('approved_for_submit', False)):
        return _notify_and_return({
            **base_result,
            'status': 'blocked',
            'detail': 'Order is still waiting for approval. Mark it approved or switch the approval workflow to automatic.',
        })

    if not bool(order.get('ready_for_broker_submit', False)):
        return _notify_and_return({
            **base_result,
            'status': 'blocked',
            'detail': 'Order preview is not ready for broker submit.',
        })

    is_option = _looks_like_occ_option_symbol(str(order.get('ticker', '')))
    if 'single_leg_option' in str(order.get('execution_style', '')).lower() and not is_option:
        return _notify_and_return({**base_result, 'status': 'blocked',
                                   'rejection_code': 'REJECTED_SAFETY_DATA_UNAVAILABLE',
                                   'detail': 'A valid option contract symbol is required.'})
    is_option_entry = is_option and _option_position_intent(
        str(order.get('proxy_side', '')), str(order.get('execution_style', ''))
    ) == 'buy_to_open'
    if is_option_entry:
        code, detail, rank_source = validate_live_option_entry(order, config)
        if code:
            return _notify_and_return({**base_result, 'status': 'blocked', 'rejection_code': code,
                                       'detail': detail, 'iv_rank_source': rank_source})
        base_result['iv_rank_source'] = rank_source

    if config.trading_mode == 'live' and not _bool_env('CONFIRM_LIVE_TRADING', False):
        return _notify_and_return({
            **base_result,
            'status': 'blocked',
            'detail': 'Live trading confirmation flag is not enabled.',
        })

    if not config.auto_submit:
        return _notify_and_return({
            **base_result,
            'status': 'preview_only',
            'detail': 'AUTO_SUBMIT is disabled. No broker order was sent.',
        })

    if config.broker != 'alpaca':
        return _notify_and_return({
            **base_result,
            'status': 'blocked',
            'detail': f"Unsupported broker: {config.broker}",
        })

    if not config.api_key or not config.secret_key:
        return _notify_and_return({
            **base_result,
            'status': 'missing_credentials',
            'detail': 'Alpaca credentials are not configured.',
        })

    try:
        symbol = str(order['ticker']).upper()
        side = str(order['proxy_side']).strip().lower()
        payload = {
            'symbol': symbol,
            'qty': int(order['proxy_qty']),
            'side': side,
            'type': 'market',
            'time_in_force': 'day',
            'client_order_id': order['client_order_id'],
        }
        if is_option_entry:
            from alpaca.data.historical import OptionHistoricalDataClient
            from .market_data import option_snapshot_metrics

            try:
                option_midpoint, spread, _ = option_snapshot_metrics(
                    OptionHistoricalDataClient(config.api_key, config.secret_key), symbol,
                    float(os.getenv('MAX_QUOTE_AGE_SECONDS', '60')),
                )
                spread_limit = float(os.getenv('MAX_OPTION_SPREAD_PCT', '0.06'))
                if not math.isfinite(spread_limit) or spread_limit < 0 or spread > spread_limit:
                    return _notify_and_return({**base_result, 'status': 'blocked',
                                               'rejection_code': 'REJECTED_SPREAD_WIDTH',
                                               'detail': 'Fresh option spread exceeds the configured limit.'})
                ask = Decimal(str(option_midpoint)) * (Decimal('1') + Decimal(str(spread)) / 2)
                limit_price = ask.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
                risk_budget = Decimal(str(order.get('max_risk_usd', 0)))
                if not limit_price.is_finite() or limit_price <= 0 or not risk_budget.is_finite() or risk_budget <= 0:
                    raise ValueError('Valid limit price and risk budget are required')
                if limit_price * int(order['proxy_qty']) * 100 > risk_budget:
                    return _notify_and_return({**base_result, 'status': 'blocked',
                                               'rejection_code': 'REJECTED_RISK_BUDGET',
                                               'detail': 'Fresh limit-order cost exceeds the configured risk budget.'})
            except (ValueError, TypeError, AttributeError, KeyError, InvalidOperation):
                return _notify_and_return({**base_result, 'status': 'blocked',
                                           'rejection_code': 'REJECTED_SAFETY_DATA_UNAVAILABLE',
                                           'detail': 'Fresh option quote or risk budget is unavailable or invalid.'})
            except Exception:
                return _notify_and_return({**base_result, 'status': 'blocked',
                                           'rejection_code': 'REJECTED_SAFETY_DATA_UNAVAILABLE',
                                           'detail': 'Fresh option quote request failed.'})
            payload['type'] = 'limit'
            payload['limit_price'] = format(limit_price, '.2f')
            base_result['limit_price'] = format(limit_price, '.2f')
        if is_option:
            payload['position_intent'] = _option_position_intent(side, str(order.get('execution_style', '')))
        response = _request_with_retries(
            method='POST',
            url=f"{config.base_url}/v2/orders",
            headers=_broker_headers(config),
            json_body=payload,
            timeout=20,
        )
        body = response.json()
        result = {
            **base_result,
            'status': body.get('status', 'accepted'),
            'detail': body.get('id', 'order_submitted'),
        }
        if config.discord_webhook_url:
            title, fields, color, status = _build_order_notification(order, result)
            notify_discord(config.discord_webhook_url, title, fields, color, status)
        return result
    except Exception as exc:
        response_text = ''
        response_obj = getattr(exc, 'response', None)
        if response_obj is not None:
            try:
                response_text = str(response_obj.text or '').strip()
            except Exception:
                response_text = ''
        detail = str(exc)
        if response_text:
            detail = f"{detail} | response={response_text}"
        result = {
            **base_result,
            'status': 'error',
            'detail': detail,
        }
        if config.discord_webhook_url:
            title, fields, color, status = _build_order_notification(order, result)
            notify_discord(config.discord_webhook_url, title, fields, color, status)
        return result


def notify_discord(
    webhook_url: str,
    title: str,
    fields: dict[str, str],
    color: int = 3447003,
    status: str = 'submitted',
) -> bool:
    """
    Send a trade notification to Discord via webhook.

    Args:
        webhook_url: Discord webhook URL from .env
        title: Main notification title (e.g., "Order Submitted", "Position Closed")
        fields: Dict of field_name -> field_value for the embed
        color: Decimal color code (default blue). Use 15158332 for green (wins), 15548997 for red (losses)
        status: "submitted" | "filled" | "rejected" | "closed_profit" | "closed_loss"

    Returns:
        True if sent successfully, False if webhook URL missing or request failed
    """
    if not webhook_url or not webhook_url.startswith('http'):
        return False

    embed = {
        'title': title,
        'color': color,
        'fields': [{'name': k, 'value': str(v), 'inline': True} for k, v in fields.items()],
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    payload = {'embeds': [embed]}

    try:
        response = requests.post(webhook_url, json=payload, timeout=5)
        if response.status_code in {200, 204}:
            return True
        print(
            'Discord notification failed '
            f'(status={response.status_code}): '
            f"{response.text[:300]}"
        )
        return False
    except Exception as exc:
        print(f'Discord notification failed: {exc}')
        return False


def _build_order_notification(order: pd.Series, result: dict[str, Any]) -> tuple[str, dict[str, str], int, str]:
    """Build Discord notification payload for a submitted/rejected order."""
    ticker = str(order.get('underlying_ticker', order.get('ticker', '')))
    proxy_side = str(order.get('proxy_side', '')).upper()
    proxy_qty = int(order.get('proxy_qty', 0))

    execution_style = str(order.get('execution_style', ''))
    is_option = 'option' in execution_style

    if is_option:
        dte = int(order.get('days_to_expiration', 0) or 0)
        strike = float(order.get('strike_price', 0.0) or 0.0)
        cost = float(order.get('estimated_contract_cost_usd', 0.0) or 0.0)
        option_type = str(order.get('option_type', '?')).upper()
        ticker_display = f'{ticker} {dte} DTE ${strike} {option_type}'
        notional = cost * proxy_qty
    else:
        price = float(order.get('last_close', 0.0) or 0.0)
        ticker_display = ticker
        notional = price * proxy_qty

    status = str(result.get('status', 'unknown')).strip().lower()

    if status in {'rejected', 'error', 'missing_credentials'}:
        color = 15548997  # Red
        title = f'⛔ Trade Rejected: {ticker_display}'
        reason = result.get('detail', 'No reason provided')
        fields = {
            'Ticker': ticker,
            'Side': proxy_side,
            'Qty': str(proxy_qty),
            'Reason': reason,
            'Time': datetime.now().strftime('%H:%M:%S'),
        }
        status_label = 'rejected'
    elif status in {'new', 'accepted', 'filled', 'partially_filled', 'pending_new', 'submitted'}:
        color = 3447003  # Blue (pending)
        title = f'✅ Trade Submitted: {ticker_display}'
        fields = {
            'Ticker': ticker,
            'Side': proxy_side,
            'Qty': str(proxy_qty),
            'Estimated Notional': f'${notional:,.2f}',
            'Status': status.title(),
            'Time': datetime.now().strftime('%H:%M:%S'),
        }
        status_label = 'submitted'
    elif status == 'skipped_existing':
        color = 6710886  # Grey
        title = f'⏭ Trade Skipped (Already Open): {ticker_display}'
        fields = {
            'Ticker': ticker,
            'Reason': 'Position already submitted or filled',
            'Time': datetime.now().strftime('%H:%M:%S'),
        }
        status_label = 'submitted'
    elif status == 'blocked':
        color = 15105570  # Orange
        title = f'🚫 Trade Blocked: {ticker_display}'
        fields = {
            'Ticker': ticker,
            'Reason': str(result.get('detail', 'Unknown block reason')),
            'Time': datetime.now().strftime('%H:%M:%S'),
        }
        status_label = 'rejected'
    elif status == 'preview_only':
        color = 8421504  # Light grey
        title = f'👁 Preview Only: {ticker_display}'
        fields = {
            'Ticker': ticker,
            'Reason': 'AUTO SUBMIT is disabled',
            'Time': datetime.now().strftime('%H:%M:%S'),
        }
        status_label = 'submitted'
    else:
        return 'Trade Preview', {}, 3447003, 'submitted'

    return title, fields, color, status_label


def _build_exit_notification(
    symbol: str,
    entry_price: float,
    exit_price: float,
    qty: int,
    side: str,
) -> tuple[str, dict[str, str], int, str]:
    """Build Discord notification payload for a closed position with P&L."""
    gross_pl = (exit_price - entry_price) * qty
    pct_pl = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

    color = 15158332 if gross_pl >= 0 else 15548997  # Green if profit, red if loss
    emoji = '🟢' if gross_pl >= 0 else '🔴'

    title = f'{emoji} Position Closed: {symbol}'
    fields = {
        'Ticker': symbol,
        'Entry Price': f'${entry_price:.2f}',
        'Exit Price': f'${exit_price:.2f}',
        'Qty': str(qty),
        'P&L': f'${gross_pl:+,.2f} ({pct_pl:+.2f}%)',
        'Time': datetime.now().strftime('%H:%M:%S'),
    }
    status_label = 'closed_profit' if gross_pl >= 0 else 'closed_loss'

    return title, fields, color, status_label
