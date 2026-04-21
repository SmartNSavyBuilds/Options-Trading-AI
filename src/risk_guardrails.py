from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


ETF_SYMBOLS = {
    'SPY', 'QQQ', 'IWM', 'DIA', 'SMH', 'XLK', 'XLF', 'XLV', 'XLY', 'XLP', 'XLI', 'XLE', 'XLB', 'XLU',
}
CRYPTO_SYMBOLS = {'BTC', 'ETH', 'SOL', 'DOGE', 'XRP'}
SECTOR_MAP = {
    'AAPL': 'Technology', 'MSFT': 'Technology', 'NVDA': 'Technology', 'AMD': 'Technology', 'AVGO': 'Technology', 'INTC': 'Technology', 'XLK': 'Technology', 'SMH': 'Technology', 'QQQ': 'Technology',
    'META': 'Communication Services', 'GOOGL': 'Communication Services', 'SNAP': 'Communication Services', 'ROKU': 'Communication Services', 'DIS': 'Communication Services',
    'AMZN': 'Consumer Discretionary', 'TSLA': 'Consumer Discretionary', 'UBER': 'Consumer Discretionary', 'XLY': 'Consumer Discretionary',
    'BAC': 'Financials', 'GS': 'Financials', 'JPM': 'Financials', 'PYPL': 'Financials', 'XLF': 'Financials',
    'UNH': 'Healthcare', 'XLV': 'Healthcare',
    'SPY': 'Broad Market', 'DIA': 'Broad Market', 'IWM': 'Broad Market',
    'BTC': 'Crypto', 'ETH': 'Crypto', 'SOL': 'Crypto', 'DOGE': 'Crypto', 'XRP': 'Crypto',
}
CORRELATION_BUCKET_MAP = {
    'AAPL': 'mega_cap_growth', 'MSFT': 'mega_cap_growth', 'NVDA': 'mega_cap_growth', 'AMD': 'mega_cap_growth', 'AVGO': 'mega_cap_growth', 'META': 'mega_cap_growth', 'GOOGL': 'mega_cap_growth', 'QQQ': 'mega_cap_growth', 'SMH': 'mega_cap_growth', 'XLK': 'mega_cap_growth',
    'SPY': 'broad_index_beta', 'DIA': 'broad_index_beta', 'IWM': 'broad_index_beta',
    'BAC': 'financial_beta', 'GS': 'financial_beta', 'JPM': 'financial_beta', 'XLF': 'financial_beta', 'PYPL': 'financial_beta',
    'UNH': 'defensive_healthcare', 'XLV': 'defensive_healthcare',
    'AMZN': 'consumer_growth', 'TSLA': 'consumer_growth', 'UBER': 'consumer_growth', 'DIS': 'consumer_growth', 'SNAP': 'consumer_growth', 'ROKU': 'consumer_growth',
    'BTC': 'crypto_beta', 'ETH': 'crypto_beta', 'SOL': 'crypto_beta', 'DOGE': 'crypto_beta', 'XRP': 'crypto_beta',
}
EXPOSURE_SUMMARY_COLUMNS = [
    'underlying_symbol',
    'sector',
    'correlation_bucket',
    'asset_family',
    'total_market_value',
    'position_count',
    'stock_positions',
    'option_positions',
    'exposure_pct',
]
RISK_OVERVIEW_COLUMNS = [
    'total_market_value',
    'total_exposure_pct',
    'stock_exposure_pct',
    'option_exposure_pct',
    'largest_position_pct',
    'largest_sector',
    'largest_correlation_bucket',
    'risk_posture',
]
STRESS_SCENARIO_COLUMNS = [
    'scenario',
    'shock_pct',
    'estimated_pnl',
    'stressed_market_value',
]


def extract_underlying_symbol(symbol: str) -> str:
    match = re.match(r'([A-Z]+)', str(symbol or '').upper())
    return match.group(1) if match else ''


def _is_option_symbol(symbol: str) -> bool:
    return bool(re.search(r'\d{6}[CP]', str(symbol or '').upper()))


def classify_symbol_profile(symbol: str) -> dict[str, str]:
    ticker = extract_underlying_symbol(symbol)
    sector = SECTOR_MAP.get(ticker, 'Other')
    correlation_bucket = CORRELATION_BUCKET_MAP.get(ticker)

    if not correlation_bucket:
        if sector == 'Technology':
            correlation_bucket = 'mega_cap_growth'
        elif sector == 'Financials':
            correlation_bucket = 'financial_beta'
        elif sector == 'Crypto':
            correlation_bucket = 'crypto_beta'
        elif sector in {'Broad Market', 'Industrials'}:
            correlation_bucket = 'broad_index_beta'
        else:
            correlation_bucket = 'idiosyncratic'

    if ticker in CRYPTO_SYMBOLS:
        asset_family = 'crypto'
    elif ticker in ETF_SYMBOLS:
        asset_family = 'equity_etf'
    else:
        asset_family = 'single_name_equity'

    return {
        'sector': sector,
        'correlation_bucket': correlation_bucket,
        'asset_family': asset_family,
    }


def build_exposure_summary(positions: pd.DataFrame, account_size_usd: float = 100_000.0) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame(columns=EXPOSURE_SUMMARY_COLUMNS)

    working = positions.copy()
    working['symbol'] = working.get('symbol', pd.Series('', index=working.index)).astype(str).str.upper()
    working['underlying_symbol'] = working['symbol'].map(extract_underlying_symbol)
    working['market_value'] = pd.to_numeric(working.get('market_value', 0.0), errors='coerce').fillna(0.0).abs()
    working['asset_class'] = working['symbol'].map(lambda value: 'option' if _is_option_symbol(value) else 'stock')

    summary = working.groupby('underlying_symbol', dropna=False).agg(
        total_market_value=('market_value', 'sum'),
        position_count=('symbol', 'count'),
    ).reset_index()

    stock_counts = working[working['asset_class'] == 'stock'].groupby('underlying_symbol').size().rename('stock_positions')
    option_counts = working[working['asset_class'] == 'option'].groupby('underlying_symbol').size().rename('option_positions')
    summary = summary.merge(stock_counts, on='underlying_symbol', how='left')
    summary = summary.merge(option_counts, on='underlying_symbol', how='left')
    summary['stock_positions'] = summary['stock_positions'].fillna(0).astype(int)
    summary['option_positions'] = summary['option_positions'].fillna(0).astype(int)
    summary['exposure_pct'] = (summary['total_market_value'] / max(float(account_size_usd or 100_000.0), 1.0) * 100).round(2)

    profiles = summary['underlying_symbol'].map(classify_symbol_profile)
    summary['sector'] = profiles.map(lambda profile: profile['sector'])
    summary['correlation_bucket'] = profiles.map(lambda profile: profile['correlation_bucket'])
    summary['asset_family'] = profiles.map(lambda profile: profile['asset_family'])

    summary = summary.sort_values(['exposure_pct', 'underlying_symbol'], ascending=[False, True]).reset_index(drop=True)
    return summary[EXPOSURE_SUMMARY_COLUMNS]


def build_risk_overview(positions: pd.DataFrame, account_size_usd: float = 100_000.0) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame(
            [{
                'total_market_value': 0.0,
                'total_exposure_pct': 0.0,
                'stock_exposure_pct': 0.0,
                'option_exposure_pct': 0.0,
                'largest_position_pct': 0.0,
                'largest_sector': 'None',
                'largest_correlation_bucket': 'None',
                'risk_posture': 'Idle',
            }],
            columns=RISK_OVERVIEW_COLUMNS,
        )

    working = positions.copy()
    working['symbol'] = working.get('symbol', pd.Series('', index=working.index)).astype(str).str.upper()
    working['market_value'] = pd.to_numeric(working.get('market_value', 0.0), errors='coerce').fillna(0.0).abs()
    working['asset_class'] = working['symbol'].map(lambda value: 'option' if _is_option_symbol(value) else 'stock')
    summary = build_exposure_summary(working, account_size_usd=account_size_usd)

    total_market_value = float(working['market_value'].sum())
    stock_market_value = float(working.loc[working['asset_class'] == 'stock', 'market_value'].sum())
    option_market_value = float(working.loc[working['asset_class'] == 'option', 'market_value'].sum())
    total_exposure_pct = round(total_market_value / max(float(account_size_usd or 100_000.0), 1.0) * 100, 2)
    stock_exposure_pct = round(stock_market_value / max(float(account_size_usd or 100_000.0), 1.0) * 100, 2)
    option_exposure_pct = round(option_market_value / max(float(account_size_usd or 100_000.0), 1.0) * 100, 2)

    largest_position_pct = float(summary['exposure_pct'].max()) if not summary.empty else 0.0
    sector_view = summary.groupby('sector', dropna=False)['exposure_pct'].sum().sort_values(ascending=False)
    bucket_view = summary.groupby('correlation_bucket', dropna=False)['exposure_pct'].sum().sort_values(ascending=False)
    largest_sector = str(sector_view.index[0]) if not sector_view.empty else 'None'
    largest_correlation_bucket = str(bucket_view.index[0]) if not bucket_view.empty else 'None'

    if total_exposure_pct >= 35 or largest_position_pct >= 10:
        risk_posture = 'Extended'
    elif total_exposure_pct >= 20 or option_exposure_pct >= 10:
        risk_posture = 'Balanced'
    else:
        risk_posture = 'Tight'

    return pd.DataFrame(
        [{
            'total_market_value': total_market_value,
            'total_exposure_pct': total_exposure_pct,
            'stock_exposure_pct': stock_exposure_pct,
            'option_exposure_pct': option_exposure_pct,
            'largest_position_pct': round(largest_position_pct, 2),
            'largest_sector': largest_sector,
            'largest_correlation_bucket': largest_correlation_bucket,
            'risk_posture': risk_posture,
        }],
        columns=RISK_OVERVIEW_COLUMNS,
    )


def build_stress_test_table(positions: pd.DataFrame) -> pd.DataFrame:
    scenarios = [
        ('Mild pullback -2%', -0.02),
        ('Risk-off -5%', -0.05),
        ('Stress event -10%', -0.10),
        ('Relief rally +3%', 0.03),
    ]
    if positions.empty:
        return pd.DataFrame(
            [{'scenario': name, 'shock_pct': shock, 'estimated_pnl': 0.0, 'stressed_market_value': 0.0} for name, shock in scenarios],
            columns=STRESS_SCENARIO_COLUMNS,
        )

    working = positions.copy()
    working['symbol'] = working.get('symbol', pd.Series('', index=working.index)).astype(str).str.upper()
    working['market_value'] = pd.to_numeric(working.get('market_value', 0.0), errors='coerce').fillna(0.0).abs()
    working['asset_class'] = working['symbol'].map(lambda value: 'option' if _is_option_symbol(value) else 'stock')
    working['beta_multiplier'] = working['asset_class'].map({'stock': 1.0, 'option': 1.8}).fillna(1.0)
    working.loc[working['symbol'].map(extract_underlying_symbol).isin(CRYPTO_SYMBOLS), 'beta_multiplier'] = 1.6

    base_market_value = float(working['market_value'].sum())
    rows: list[dict[str, float | str]] = []
    for name, shock in scenarios:
        estimated_pnl = float((working['market_value'] * shock * working['beta_multiplier']).sum())
        rows.append(
            {
                'scenario': name,
                'shock_pct': shock,
                'estimated_pnl': round(estimated_pnl, 2),
                'stressed_market_value': round(base_market_value + estimated_pnl, 2),
            }
        )
    return pd.DataFrame(rows, columns=STRESS_SCENARIO_COLUMNS)


def apply_exposure_guardrails(
    candidates: pd.DataFrame,
    positions: pd.DataFrame | None = None,
    account_size_usd: float = 100_000.0,
    max_total_exposure_pct: float = 35.0,
    max_single_name_exposure_pct: float = 8.0,
    max_queue_risk_usd: float = 5_000.0,
    max_positions: int = 8,
    max_sector_exposure_pct: float = 100.0,
    max_correlation_bucket_exposure_pct: float = 100.0,
    max_options_exposure_pct: float = 20.0,
) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()

    positions = positions.copy() if positions is not None else pd.DataFrame(columns=['symbol', 'market_value'])
    if not positions.empty:
        positions['symbol'] = positions.get('symbol', pd.Series('', index=positions.index)).astype(str).str.upper()
        positions['market_value'] = pd.to_numeric(positions.get('market_value', 0.0), errors='coerce').fillna(0.0).abs()
        positions['asset_class'] = positions['symbol'].map(lambda value: 'option' if _is_option_symbol(value) else 'stock')

    exposure_summary = build_exposure_summary(positions, account_size_usd=account_size_usd)
    exposure_map = exposure_summary.set_index('underlying_symbol')['exposure_pct'].to_dict() if not exposure_summary.empty else {}
    sector_exposure_map = exposure_summary.groupby('sector')['exposure_pct'].sum().to_dict() if not exposure_summary.empty else {}
    bucket_exposure_map = exposure_summary.groupby('correlation_bucket')['exposure_pct'].sum().to_dict() if not exposure_summary.empty else {}

    current_total_exposure_pct = 0.0
    current_options_exposure_pct = 0.0
    if not positions.empty and 'market_value' in positions.columns:
        denominator = max(float(account_size_usd or 100_000.0), 1.0)
        current_total_exposure_pct = float(positions['market_value'].sum()) / denominator * 100
        current_options_exposure_pct = float(positions.loc[positions['asset_class'] == 'option', 'market_value'].sum()) / denominator * 100

    running_total_exposure_pct = current_total_exposure_pct
    running_options_exposure_pct = current_options_exposure_pct
    running_queue_risk_usd = 0.0
    accepted_rows: list[dict] = []

    ranked = candidates.sort_values(['rank_score', 'projected_return_pct'], ascending=[False, False]).copy()
    for _, row in ranked.iterrows():
        record = row.to_dict()
        ticker = str(record.get('ticker', '')).upper()
        allocation_pct = float(record.get('allocation_pct', 0.0) or 0.0)
        max_risk_usd = float(record.get('max_risk_usd', 0.0) or 0.0)
        current_name_exposure_pct = float(exposure_map.get(ticker, 0.0) or 0.0)
        profile = classify_symbol_profile(ticker)
        sector = profile['sector']
        correlation_bucket = profile['correlation_bucket']
        asset_family = profile['asset_family']
        candidate_asset_class = 'option' if str(record.get('options_setup', '')).strip() not in {'', 'No trade'} else 'stock'

        current_sector_exposure_pct = float(sector_exposure_map.get(sector, 0.0) or 0.0)
        current_bucket_exposure_pct = float(bucket_exposure_map.get(correlation_bucket, 0.0) or 0.0)
        projected_total_exposure_pct = running_total_exposure_pct + allocation_pct
        projected_sector_exposure_pct = current_sector_exposure_pct + allocation_pct
        projected_bucket_exposure_pct = current_bucket_exposure_pct + allocation_pct
        projected_options_exposure_pct = running_options_exposure_pct + allocation_pct if candidate_asset_class == 'option' else running_options_exposure_pct

        guardrail_status = 'pass'
        reason = 'Eligible under current exposure, sector, and correlation limits.'

        if current_name_exposure_pct >= max_single_name_exposure_pct:
            guardrail_status = 'blocked_single_name'
            reason = 'Single-name exposure cap has already been reached for this ticker.'
        elif projected_total_exposure_pct > max_total_exposure_pct:
            guardrail_status = 'blocked_total_exposure'
            reason = 'Adding this idea would push the portfolio above the total exposure cap.'
        elif projected_sector_exposure_pct > max_sector_exposure_pct:
            guardrail_status = 'blocked_sector'
            reason = f'Adding this idea would over-concentrate the portfolio in the {sector} sector.'
        elif projected_bucket_exposure_pct > max_correlation_bucket_exposure_pct:
            guardrail_status = 'blocked_correlation'
            reason = f'Adding this idea would over-cluster exposure in the {correlation_bucket} correlation bucket.'
        elif projected_options_exposure_pct > max_options_exposure_pct:
            guardrail_status = 'blocked_asset_class'
            reason = 'Adding this idea would push option-linked exposure above the options cap.'
        elif running_queue_risk_usd + max_risk_usd > max_queue_risk_usd:
            guardrail_status = 'blocked_queue_risk'
            reason = 'Adding this idea would exceed the queue risk budget for this cycle.'

        record['sector'] = sector
        record['correlation_bucket'] = correlation_bucket
        record['asset_family'] = asset_family
        record['current_name_exposure_pct'] = round(current_name_exposure_pct, 2)
        record['portfolio_exposure_after_trade_pct'] = round(projected_total_exposure_pct, 2)
        record['current_sector_exposure_pct'] = round(current_sector_exposure_pct, 2)
        record['sector_exposure_after_trade_pct'] = round(projected_sector_exposure_pct, 2)
        record['current_bucket_exposure_pct'] = round(current_bucket_exposure_pct, 2)
        record['bucket_exposure_after_trade_pct'] = round(projected_bucket_exposure_pct, 2)
        record['guardrail_status'] = 'pass' if guardrail_status == 'pass' else 'blocked'
        record['guardrail_reason'] = reason

        if guardrail_status == 'pass':
            running_total_exposure_pct = projected_total_exposure_pct
            running_queue_risk_usd += max_risk_usd
            if candidate_asset_class == 'option':
                running_options_exposure_pct = projected_options_exposure_pct
            sector_exposure_map[sector] = projected_sector_exposure_pct
            bucket_exposure_map[correlation_bucket] = projected_bucket_exposure_pct
            accepted_rows.append(record)

        if len(accepted_rows) >= max_positions:
            break

    if not accepted_rows:
        return pd.DataFrame(columns=list(ranked.columns) + [
            'sector', 'correlation_bucket', 'asset_family',
            'current_name_exposure_pct', 'portfolio_exposure_after_trade_pct',
            'current_sector_exposure_pct', 'sector_exposure_after_trade_pct',
            'current_bucket_exposure_pct', 'bucket_exposure_after_trade_pct',
            'guardrail_status', 'guardrail_reason'
        ])
    return pd.DataFrame(accepted_rows)


def save_exposure_summary(project_dir: Path, summary: pd.DataFrame) -> Path:
    output_dir = project_dir / 'outputs'
    output_dir.mkdir(exist_ok=True)
    path = output_dir / 'exposure_summary.csv'
    summary.to_csv(path, index=False)
    return path


def save_risk_outputs(project_dir: Path, overview: pd.DataFrame, stress_table: pd.DataFrame) -> tuple[Path, Path]:
    output_dir = project_dir / 'outputs'
    output_dir.mkdir(exist_ok=True)
    overview_path = output_dir / 'risk_overview.csv'
    stress_path = output_dir / 'stress_scenarios.csv'
    overview.to_csv(overview_path, index=False)
    stress_table.to_csv(stress_path, index=False)
    return overview_path, stress_path
