from __future__ import annotations

from typing import Dict, List

import pandas as pd

from .signal_engine import compute_rsi


def backtest_directional_signals(market_data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: List[dict] = []

    for ticker, df in market_data.items():
        if df.empty or len(df) < 40:
            continue

        test_df = df.copy()
        close = test_df['Close'].astype(float)
        sma_10 = close.rolling(10).mean()
        sma_20 = close.rolling(20).mean()
        rsi = compute_rsi(close)

        predictions = []
        outcomes = []
        for i in range(21, len(test_df) - 1):
            score = 0
            if close.iloc[i] > sma_10.iloc[i] > sma_20.iloc[i]:
                score += 2
            elif close.iloc[i] < sma_10.iloc[i] < sma_20.iloc[i]:
                score -= 2

            if rsi.iloc[i] > 65:
                score += 1
            elif rsi.iloc[i] < 35:
                score -= 1

            if score >= 2:
                predictions.append(1)
            elif score <= -2:
                predictions.append(-1)
            else:
                predictions.append(0)

            next_return = close.iloc[i + 1] - close.iloc[i]
            outcomes.append(1 if next_return > 0 else -1 if next_return < 0 else 0)

        actionable = [(p, o) for p, o in zip(predictions, outcomes) if p != 0]
        if not actionable:
            continue

        wins = sum(1 for p, o in actionable if p == o)
        accuracy = wins / len(actionable)
        rows.append({
            'ticker': ticker,
            'trades': len(actionable),
            'win_rate': round(accuracy, 3)
        })

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values('win_rate', ascending=False)
    return result
