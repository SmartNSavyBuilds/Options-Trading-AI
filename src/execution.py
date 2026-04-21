from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)

PAPER_BASE_URL = 'https://paper-api.alpaca.markets'
LIVE_BASE_URL = 'https://api.alpaca.markets'


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
        api_key = os.getenv('ALPACA_API_KEY', '').strip()
        secret_key = os.getenv('ALPACA_SECRET_KEY', '').strip()
        default_base_url = PAPER_BASE_URL if trading_mode == 'paper' else LIVE_BASE_URL
        base_url = os.getenv('ALPACA_BASE_URL', default_base_url).strip() or default_base_url

        return cls(
            broker=broker,
            trading_mode=trading_mode,
            auto_submit=auto_submit,
            approval_mode=approval_mode,
            use_proxy_equities=use_proxy_equities,
            account_size_usd=account_size_usd,
            max_positions=max_positions,
            max_position_pct=max_position_pct,
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


def _resolve_submit_approval(row: pd.Series, config: TradingConfig) -> tuple[str, bool]:
    approval_status = str(row.get('approval_status', 'pending') or 'pending').strip().lower()
    approved_for_submit = approval_status in {'approved', 'auto_approved'} or _as_bool(row.get('approved_for_submit', False))

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
    import re
    match = re.search(r'(\d+(?:\.\d+)?)\s*[CP]', text.upper())
    if match:
        strike = float(match.group(1))
    return option_type, strike


def fetch_option_contracts(config: TradingConfig, underlying_symbol: str, max_days: int = 10, limit: int = 200) -> pd.DataFrame:
    if config.broker != 'alpaca' or not config.api_key or not config.secret_key:
        return pd.DataFrame()

    today = pd.Timestamp.now().normalize()
    expiry_gte = (today + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
    expiry_lte = (today + pd.Timedelta(days=max_days)).strftime('%Y-%m-%d')

    response = requests.get(
        f"{config.base_url}/v2/options/contracts",
        headers=_broker_headers(config),
        params={
            'underlying_symbols': underlying_symbol,
            'status': 'active',
            'expiration_date_gte': expiry_gte,
            'expiration_date_lte': expiry_lte,
            'limit': limit,
        },
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    contracts = pd.DataFrame(body.get('option_contracts', []))
    if contracts.empty:
        return contracts

    contracts['expiration_date'] = pd.to_datetime(contracts['expiration_date'], errors='coerce')
    today = pd.Timestamp.now().normalize()
    contracts['days_to_expiration'] = (contracts['expiration_date'] - today).dt.days
    contracts = contracts[(contracts['days_to_expiration'] >= 1) & (contracts['days_to_expiration'] <= max_days)].copy()
    return contracts


def choose_best_option_contract(
    contracts: pd.DataFrame,
    underlying_symbol: str,
    option_type: str,
    target_strike: float,
    max_days: int = 10,
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

    working = working[(working['days_to_expiration'] >= 1) & (working['days_to_expiration'] <= max_days)]
    if working.empty:
        return None

    working['strike_price'] = pd.to_numeric(working.get('strike_price'), errors='coerce').fillna(0.0)
    if 'open_interest' not in working.columns:
        working['open_interest'] = 0
    working['open_interest'] = pd.to_numeric(working['open_interest'], errors='coerce').fillna(0)
    working['strike_distance'] = (working['strike_price'] - float(target_strike or 0.0)).abs()
    working = working.sort_values(['days_to_expiration', 'strike_distance', 'open_interest'], ascending=[True, True, False])
    return working.iloc[0].to_dict()


def prepare_option_execution_preview(
    queue: pd.DataFrame,
    config: TradingConfig,
    contracts_map: dict[str, pd.DataFrame] | None = None,
    max_days: int = 10,
    top_n: int = 2,
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

    preview_rows: list[dict[str, Any]] = []
    for _, row in working.iterrows():
        if len(preview_rows) >= top_n:
            break
        approval_status, approved_for_submit = _resolve_submit_approval(row, config)
        existing_status = str(row.get('option_order_status', 'queued_for_review')).strip().lower()
        already_submitted = existing_status in {'new', 'accepted', 'filled', 'partially_filled', 'submitted', 'pending_new'}
        underlying = str(row.get('ticker', '')).upper()
        option_type, target_strike = _infer_option_type_and_strike(str(row.get('options_setup', '')), str(row.get('bias', 'neutral')))

        contracts = contracts_map.get(underlying, pd.DataFrame()) if contracts_map else fetch_option_contracts(config, underlying, max_days=max_days)
        chosen = choose_best_option_contract(contracts, underlying_symbol=underlying, option_type=option_type, target_strike=target_strike, max_days=max_days)
        if not chosen:
            continue

        close_price = float(chosen.get('close_price', 0.0) or 0.0)
        estimated_contract_cost = max(close_price * 100, 1.0)
        target_notional = config.account_size_usd * (float(row.get('allocation_pct', 1.0) or 1.0) / 100.0)
        qty = max(1, min(2, math.floor(target_notional / estimated_contract_cost))) if estimated_contract_cost > 0 else 1
        client_order_id = f"{config.trading_mode}-option-{underlying}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        preview_rows.append({
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'ticker': str(chosen.get('symbol', underlying)),
            'underlying_ticker': underlying,
            'bias': row.get('bias', 'neutral'),
            'proxy_side': 'buy',
            'proxy_qty': int(qty),
            'signal_score': int(row.get('signal_score', 0)),
            'projected_return_pct': float(row.get('projected_return_pct', 0.0)),
            'max_risk_usd': float(row.get('max_risk_usd', 0.0)),
            'options_setup': row.get('options_setup', 'No trade'),
            'contract_name': chosen.get('name', ''),
            'expiration_date': str(chosen.get('expiration_date', ''))[:10],
            'strike_price': float(chosen.get('strike_price', 0.0) or 0.0),
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
        already_submitted = existing_status in {'new', 'accepted', 'filled', 'partially_filled', 'submitted', 'pending_new'}
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


def run_option_execution_cycle(config: TradingConfig, max_days: int = 10, top_n: int = 2) -> tuple[pd.DataFrame, pd.DataFrame]:
    queue = load_latest_csv('paper_trade_queue')
    preview = prepare_option_execution_preview(queue, config, max_days=max_days, top_n=top_n)
    results = submit_orders(preview, config)
    update_queue_after_execution(results)
    return preview, results


def prepare_exit_execution_preview(
    recommendations: pd.DataFrame,
    positions: pd.DataFrame,
    config: TradingConfig,
) -> pd.DataFrame:
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

    merged = working.merge(
        positions[['symbol', 'side', 'qty', 'market_value', 'unrealized_pl']] if not positions.empty else pd.DataFrame(columns=['symbol', 'side', 'qty', 'market_value', 'unrealized_pl']),
        on='symbol',
        how='left',
        suffixes=('', '_position'),
    )

    exit_enabled = _bool_env('ENABLE_EXIT_AUTOMATION', False) and config.trading_mode == 'paper'
    preview_rows: list[dict[str, Any]] = []
    for _, row in merged.iterrows():
        action = str(row.get('action', 'hold')).strip().lower()
        if action == 'hold':
            continue

        symbol = str(row.get('symbol', '')).upper()
        side = str(row.get('side', row.get('side_position', ''))).strip().lower()
        qty = float(row.get('qty', row.get('qty_position', 0.0)) or 0.0)
        exit_pct = min(max(float(row.get('exit_pct', 100.0) or 100.0), 0.0), 100.0)
        exit_qty = max(1, math.ceil(qty * (exit_pct / 100.0))) if qty > 0 else 0
        exit_side = 'sell' if side == 'long' else 'buy'
        approved = _as_bool(row.get('exit_approved', False))
        existing_status = str(row.get('order_status', 'monitor')).strip().lower()
        already_submitted = existing_status in {'new', 'accepted', 'filled', 'partially_filled', 'submitted', 'pending_new'}
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
    results = submit_orders(preview, config)
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

    queue.to_csv(queue_path, index=False)


def _broker_headers(config: TradingConfig) -> dict[str, str]:
    return {
        'APCA-API-KEY-ID': config.api_key,
        'APCA-API-SECRET-KEY': config.secret_key,
        'Content-Type': 'application/json',
    }


def _fetch_alpaca_json(config: TradingConfig, endpoint: str) -> Any:
    response = requests.get(
        f"{config.base_url}{endpoint}",
        headers=_broker_headers(config),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def sync_broker_state(config: TradingConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    timestamp = datetime.now(timezone.utc).isoformat()
    account_columns = [
        'synced_at_utc', 'broker', 'mode', 'connection_status', 'market_status',
        'equity', 'cash', 'buying_power', 'portfolio_value',
        'options_approved_level', 'options_trading_level', 'options_buying_power',
        'detail'
    ]
    position_columns = ['synced_at_utc', 'symbol', 'side', 'qty', 'market_value', 'unrealized_pl']
    order_columns = ['synced_at_utc', 'symbol', 'side', 'qty', 'status', 'submitted_at', 'client_order_id']

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
        account = pd.DataFrame([{
            'synced_at_utc': timestamp,
            'broker': config.broker,
            'mode': config.trading_mode,
            'connection_status': 'error',
            'market_status': 'unknown',
            'equity': 0.0,
            'cash': 0.0,
            'buying_power': 0.0,
            'portfolio_value': 0.0,
            'options_approved_level': 0,
            'options_trading_level': 0,
            'options_buying_power': 0.0,
            'detail': str(exc),
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
            })
        positions = pd.DataFrame(positions_rows, columns=position_columns)
    except Exception:
        positions = pd.DataFrame(columns=position_columns)

    try:
        orders_body = _fetch_alpaca_json(config, '/v2/orders?status=all&limit=50&direction=desc')
        order_rows = []
        for item in orders_body:
            order_rows.append({
                'synced_at_utc': timestamp,
                'symbol': item.get('symbol', ''),
                'side': item.get('side', ''),
                'qty': float(item.get('qty', 0.0) or 0.0),
                'status': item.get('status', ''),
                'submitted_at': item.get('submitted_at', ''),
                'client_order_id': item.get('client_order_id', ''),
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
    match = re.search(r'-(?:option|exit)-([A-Z]+)-', text)
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
        ticker = str(order.get('symbol', '')).upper()
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

        is_option_order = '-option-' in str(client_order_id)
        status_col = 'option_order_status' if is_option_order else 'order_status'
        time_col = 'last_option_submitted_at' if is_option_order else 'last_submitted_at'
        queue.loc[mask, status_col] = status
        queue.loc[mask, time_col] = submitted_at

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
        status = str(result.get('status', '')).strip().lower()
        submitted_at = str(result.get('submitted_at_utc', ''))
        mask = recommendations['symbol'].astype(str).str.upper() == ticker
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

    exit_orders = orders.copy()
    if 'client_order_id' in exit_orders.columns:
        exit_orders = exit_orders[exit_orders['client_order_id'].astype(str).str.contains('-exit-', case=False, na=False)]

    for _, order in exit_orders.iterrows():
        ticker = str(order.get('symbol', '')).upper()
        status = str(order.get('status', '')).strip().lower()
        submitted_at = order.get('submitted_at', '')
        mask = recommendations['symbol'].astype(str).str.upper() == ticker
        if not mask.any():
            continue
        recommendations.loc[mask, 'order_status'] = status
        recommendations.loc[mask, 'last_submitted_at'] = submitted_at

    recommendations.to_csv(exit_path, index=False)


def _save_broker_snapshots(account: pd.DataFrame, positions: pd.DataFrame, orders: pd.DataFrame) -> None:
    account.to_csv(OUTPUT_DIR / 'broker_account_status.csv', index=False)
    positions.to_csv(OUTPUT_DIR / 'broker_positions.csv', index=False)
    orders.to_csv(OUTPUT_DIR / 'broker_orders.csv', index=False)


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

    if _as_bool(order.get('already_submitted', False)):
        return {
            **base_result,
            'status': 'skipped_existing',
            'detail': 'Order for this ticker was already submitted previously and was skipped.',
        }

    if not _as_bool(order.get('approved_for_submit', False)):
        return {
            **base_result,
            'status': 'blocked',
            'detail': 'Order is still waiting for approval. Mark it approved or switch the approval workflow to automatic.',
        }

    if not bool(order.get('ready_for_broker_submit', False)):
        return {
            **base_result,
            'status': 'blocked',
            'detail': 'Order preview is not ready for broker submit.',
        }

    if config.trading_mode == 'live' and not _bool_env('CONFIRM_LIVE_TRADING', False):
        return {
            **base_result,
            'status': 'blocked',
            'detail': 'Live trading confirmation flag is not enabled.',
        }

    if not config.auto_submit:
        return {
            **base_result,
            'status': 'preview_only',
            'detail': 'AUTO_SUBMIT is disabled. No broker order was sent.',
        }

    if config.broker != 'alpaca':
        return {
            **base_result,
            'status': 'blocked',
            'detail': f"Unsupported broker: {config.broker}",
        }

    if not config.api_key or not config.secret_key:
        return {
            **base_result,
            'status': 'missing_credentials',
            'detail': 'Alpaca credentials are not configured.',
        }

    try:
        payload = {
            'symbol': order['ticker'],
            'qty': int(order['proxy_qty']),
            'side': order['proxy_side'],
            'type': 'market',
            'time_in_force': 'day',
            'client_order_id': order['client_order_id'],
        }
        response = requests.post(
            f"{config.base_url}/v2/orders",
            headers=_broker_headers(config),
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        return {
            **base_result,
            'status': body.get('status', 'accepted'),
            'detail': body.get('id', 'order_submitted'),
        }
    except Exception as exc:
        return {
            **base_result,
            'status': 'error',
            'detail': str(exc),
        }
