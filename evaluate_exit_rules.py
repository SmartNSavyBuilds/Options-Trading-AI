from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from src.exit_manager import build_exit_recommendations, save_exit_recommendations


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / 'outputs'


def _load_latest_output(prefix: str) -> pd.DataFrame:
    matches = sorted(OUTPUT_DIR.glob(f'{prefix}*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        return pd.DataFrame()
    return pd.read_csv(matches[0])


def _preserve_existing_state(recommendations: pd.DataFrame) -> pd.DataFrame:
    output_path = OUTPUT_DIR / 'exit_recommendations.csv'
    if recommendations.empty or not output_path.exists():
        return recommendations

    previous = pd.read_csv(output_path)
    if previous.empty or 'symbol' not in previous.columns:
        return recommendations

    for col, default in {
        'exit_approved': False,
        'order_status': 'monitor',
        'last_submitted_at': '',
    }.items():
        if col not in previous.columns:
            previous[col] = default
        state_map = previous.set_index('symbol')[col].to_dict()
        recommendations[col] = recommendations['symbol'].map(state_map).fillna(recommendations[col])

    return recommendations


def apply_automatic_exit_guardrails(recommendations: pd.DataFrame, enable_auto_approve: bool = False) -> pd.DataFrame:
    if recommendations.empty:
        return recommendations

    working = recommendations.copy()
    if 'exit_approved' not in working.columns:
        working['exit_approved'] = False
    if 'order_status' not in working.columns:
        working['order_status'] = 'monitor'

    if not enable_auto_approve:
        return working

    actionable = working['action'].astype(str).str.lower().isin(['stop_out', 'cover_or_reduce', 'reduce_or_close'])
    urgent_option = pd.to_numeric(working.get('days_to_expiration', ''), errors='coerce').fillna(99).le(3) & working.get('asset_class', pd.Series('', index=working.index)).astype(str).str.lower().eq('option')
    auto_mask = actionable | urgent_option
    working.loc[auto_mask, 'exit_approved'] = True
    working.loc[auto_mask, 'order_status'] = 'auto_approved'
    return working


def main() -> None:
    positions = _load_latest_output('broker_positions')
    signals = _load_latest_output('latest_signals')
    recommendations = build_exit_recommendations(positions, signals)
    recommendations = _preserve_existing_state(recommendations)
    auto_approve = str(os.getenv('AUTO_APPROVE_URGENT_EXITS', 'true')).strip().lower() in {'1', 'true', 'yes', 'on'}
    recommendations = apply_automatic_exit_guardrails(recommendations, enable_auto_approve=auto_approve)
    output_path = save_exit_recommendations(PROJECT_DIR, recommendations)

    print('Exit recommendations generated:')
    print(output_path)
    print()
    if recommendations.empty:
        print('No open positions were available for exit evaluation.')
    else:
        print(recommendations.to_string(index=False))


if __name__ == '__main__':
    main()
